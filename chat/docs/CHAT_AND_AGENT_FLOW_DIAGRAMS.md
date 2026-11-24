# Chat and agent flow: diagrams

Diagrams of the current chat flow. The text version, with every step
explained, is [CHAT_AND_AGENT_FLOW.md](CHAT_AND_AGENT_FLOW.md).

The diagrams are Mermaid. They render on GitHub/GitLab and in VS Code with a
Mermaid preview extension (for example "Markdown Preview Mermaid Support").

> Normal RAG is hidden in the chat page for now: every message goes to an agent.

---

## 1. The big picture

```mermaid
flowchart LR
    U(["User"]) --> P["Chat page<br/>chat_page.html"]
    P -- "GET /chat/agents-json/" --> AJ["views.agents_json"]
    P -- "POST /chat/&lt;session&gt;/selected-agent/" --> SA["views.selected_agent"]
    P -- "POST /chat/&lt;agent slug&gt;/<br/>session_id, question, files" --> SO["views.session_or_agent"]
    SO --> SEND["views.send / _send"]
    SEND --> R["router.find_agent<br/>router.service_for"]
    SEND --> PF["agent_capabilities.preflight"]
    R --> ACS["AgentChatService<br/>agent/service.py"]
    ACS --> G["LangGraph graph<br/>agent/graph.py"]
    G --> T["Tools<br/>agent/tools.py"]
    ACS --> LF[("Langfuse<br/>role + model config")]
    ACS --> CP[("Postgres<br/>LangGraph checkpoints")]
    ACS --> CS["ChatService.create_turn<br/>chat_service.py"]
    CS --> DB[("Postgres<br/>chat turns")]
    AJ --> AG[("agents_agent")]
    SA --> SES[("chat_conversation_session<br/>selected_agent_id")]
    ACS -- "SSE: delta, trace, done" --> P
```

---

## 2. Choosing an agent in the page

```mermaid
sequenceDiagram
    actor U as User
    participant P as Chat page
    participant V as chat/views.py
    participant DB as Postgres

    P->>V: GET /chat/agents-json/?company=ID
    V->>DB: agents of the company + global agents
    V-->>P: [{id, name, slug: "/chat/chat_agent/"}, ...]
    Note over P: Dropdown = agents only (no Normal RAG).<br/>Default: the chat's saved agent, else the first one.<br/>No agents: "No agent available", sending blocked.

    U->>P: picks "chat agent 2"
    P->>V: POST /chat/SESSION/selected-agent/ {agent_id}
    V->>DB: session.selected_agent_id = agent id
    V-->>P: {selected_agent_id}
    Note over P,DB: The saved agent only pre-fills the dropdown<br/>and guards the file panel buttons.<br/>The send URL decides who answers.
```

---

## 3. Sending a message (end to end)

```mermaid
sequenceDiagram
    actor U as User
    participant P as Chat page
    participant V as views.send / _send
    participant R as router
    participant C as agent_capabilities
    participant A as AgentChatService
    participant L as Langfuse
    participant G as LangGraph graph
    participant LLM as LLM (DeepSeek / OpenAI)
    participant DB as Postgres

    U->>P: "create a pie chart" (+ file)
    P->>V: POST /chat/chat-agent-2/<br/>session_id, question, attachment_ids<br/>Accept: text/event-stream
    Note over P: console: [chat] send -> agent=/chat/chat-agent-2/
    V->>DB: load chat (session_id)
    V->>R: find_agent(company, "/chat/chat-agent-2/")
    R-->>V: Agent (or 404)
    V->>DB: remember session.selected_agent
    V->>C: preflight(agent, message, files)
    C-->>V: pipeline / fallback / blocked
    V->>R: service_for(agent)
    R-->>V: AgentChatService
    V->>A: stream_message(...)
    A->>L: role + model config (prompt "chat-agent-2")
    A->>DB: files for this message (CSV rows + value counts)
    A->>DB: resume thread "SESSION:AGENT" (checkpoint)
    A->>G: stream(messages, context)
    loop until the model answers (max 12 steps)
        G->>LLM: system prompt + messages + step tools
        LLM-->>G: tool call, or answer tokens
        G->>G: run tools (create_chart, document_search, ...)
    end
    G-->>A: tokens, tool calls, tool results
    A-->>P: SSE delta (answer text) / trace (Agent steps)
    A->>DB: save turn: answer, chart, citations,<br/>tools_used, trace, checkpoint_id, thread_id
    A-->>P: SSE done (user + assistant message)
    Note over P: console: answered by agent=... route=pipeline<br/>(X-Chat-Agent / X-Chat-Route headers)
    P->>U: answer + chart + Agent steps
```

---

## 4. Which path answers (capability check)

```mermaid
flowchart TD
    S["POST /chat/&lt;slug&gt;/"] --> SID{"session_id<br/>in body?"}
    SID -- no --> E400["400 session_id is required"]
    SID -- yes --> EMPTY{"question, file<br/>or picked file?"}
    EMPTY -- no --> E400b["400 Type a message or attach a file"]
    EMPTY -- yes --> FIND{"agent with this slug<br/>for the company?"}
    FIND -- no --> E404["404 No agent at '/chat/...'"]
    FIND -- yes --> REM["save session.selected_agent"]
    REM --> PRE["preflight: classify the message,<br/>compare with the agent's tools"]
    PRE --> FLOW{"agent has a<br/>runnable flow?"}
    FLOW -- no --> BLK["blocked: fixed reply, no LLM"]
    FLOW -- yes --> KIND{"message kind"}
    KIND -- "action: process / stop files" --> ACT{"has the<br/>file tool?"}
    ACT -- no --> BLK
    ACT -- yes --> PIPE
    KIND -- "read: status, summary,<br/>web, documents" --> RD{"has the<br/>tool?"}
    RD -- yes --> PIPE
    RD -- "no, but the message has /<br/>refers to a chat file" --> PIPE
    RD -- no --> FB["fallback: Normal RAG answers"]
    KIND -- "other (chat, charts...)" --> PIPE["pipeline"]
    PIPE --> FILEONLY{"file sent<br/>with no message?"}
    FILEONLY -- yes --> ASK["fixed reply: what should<br/>I do with this file?"]
    FILEONLY -- no --> RUN["AgentChatService runs the agent"]
```

---

## 5. Inside the agent run

```mermaid
flowchart TD
    START(["stream_message"]) --> CHK["check user, chat, agent company"]
    CHK --> RT["role + model from Langfuse"]
    RT --> MF{"files for this message"}
    MF -- "attached" --> F1["attached files"]
    MF -- "refers to a chat file" --> F2["name match / candidates /<br/>newest ('this file')"]
    MF -- "none" --> F3["no file content"]
    F1 --> CTX
    F2 --> CTX
    F3 --> CTX
    CTX["AgentContext: role, model, file list,<br/>file content (CSV + value counts), tools"] --> GR["get_graph(Agent.graph)"]
    GR --> MEM{"this agent saved a turn<br/>in thread SESSION:AGENT?"}
    MEM -- yes --> FORK["continue from its checkpoint;<br/>other agents' turns since then<br/>= one quoted context block"]
    MEM -- no --> SEED["new thread; recent turns<br/>= one quoted context block"]
    FORK --> RUNG
    SEED --> RUNG
    RUNG["graph.stream"] --> MN["model node: system prompt + messages,<br/>step's tools bound"]
    MN --> TC{"model asks<br/>for a tool?"}
    TC -- yes --> TN["tools node runs it<br/>(errors go back to the model)"]
    TN --> MN
    TC -- no --> NEXT{"more steps?"}
    NEXT -- yes --> MN
    NEXT -- no --> ANS["answer"]
    ANS --> STR["stream deltas<br/>(a chart JSON block is not streamed)"]
    STR --> SAVE["save turn via ChatService.create_turn:<br/>answer, chart, citations, tools_used,<br/>trace, checkpoint_id, thread_id"]
    SAVE --> DONE(["SSE done"])
```

---

## 6. The system prompt, in order

```mermaid
flowchart TB
    A["1. Agent role (Langfuse prompt)"] --> B["2. Real provider + model name"]
    B --> C["3. Plain-text formatting rule"]
    C --> D{"step has tools?"}
    D -- yes --> E["4. Tool rules: only the tools you have now"]
    D -- no --> F
    E --> F{"step has create_chart?"}
    F -- yes --> G["5a. Chart: call create_chart"]
    F -- no --> H["5b. Chart: write a chart JSON block"]
    G --> I["6. Chart data: no topic / no file named →<br/>ask which file; never invent numbers"]
    H --> I
    I --> J["7. Task: finish the job, don't offer options"]
    J --> K["8. Chat's file list"]
    K --> L["9. Content of this message's files"]
```

---

## 7. How a chart is made

```mermaid
flowchart TD
    Q["chart request"] --> DATA{"what data?"}
    DATA -- "no topic, no file,<br/>nothing attached, not a follow-up" --> ASK["ask which file<br/>(lists the chat's files)"]
    DATA -- "general topic<br/>(top 5 economies)" --> GK["general knowledge,<br/>title says '(general knowledge)'"]
    DATA -- "attached / named / 'this' file<br/>or a follow-up" --> FILE["file content with exact value counts"]
    GK --> HOW
    FILE --> HOW{"which chart tool<br/>does the step have?"}
    HOW -- "chart_from_file" --> CFF["counts the CSV itself"]
    HOW -- "create_chart" --> CC["model passes the numbers"]
    HOW -- "none" --> JSON["model writes a chart JSON block"]
    JSON --> PARSE["chart_from_reply(): validate with ChartData,<br/>remove the JSON from the text"]
    CFF --> SAVE["turn.chart_data"]
    CC --> SAVE
    PARSE --> SAVE
    SAVE --> UI["page draws the chart<br/>(View large / Download chart)"]
```

---

## 8. Data that is stored

```mermaid
erDiagram
    AGENT ||--o{ STEP : "has"
    STEP }o--o{ TOOL : "binds"
    AGENT ||--o{ CONVERSATION_SESSION : "selected by"
    CONVERSATION_SESSION ||--o{ CONVERSATION : "turns"
    CONVERSATION_SESSION ||--o{ CHECKPOINT : "one thread per agent"

    AGENT {
        ulid id PK
        string slug "complete route, /chat/chat_agent/"
        string name
        json graph "flow chat runs"
        ulid company_id FK "null = global"
    }
    STEP {
        string slug
        string name
        bool connected_from_start
        bool connected_to_end
    }
    TOOL {
        string slug "create_chart, document_search, ..."
        text description "what the model reads"
        bool is_enabled
    }
    CONVERSATION_SESSION {
        ulid session_id PK
        ulid selected_agent_id FK "dropdown memory"
        json context "chat files"
    }
    CONVERSATION {
        text question
        text response
        json context "chart, citations"
        json metadata "agent_selection, tools_used, trace, checkpoint_id, thread_id"
    }
    CHECKPOINT {
        string thread_id "SESSION:AGENT"
        string checkpoint_id
    }
```

The agent's role text and model config are not in the database: they are the
Langfuse prompt named after the slug's name part (`chat_agent`).
