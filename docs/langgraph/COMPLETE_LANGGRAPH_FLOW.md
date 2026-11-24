# How chat works: Normal RAG and Agents (LangGraph)

This page follows one chat message from the browser to the saved answer and
names the file and function behind each step.

## 0. The idea in one picture

```
                         ┌──────────── chat/services/ ────────────┐
Browser                  │                                        │
 dropdown = "chatting"   │  router.py                             │
   │                     │   ├─ no slug   ─► ChatService          │  normal RAG
   ▼                     │   │              (chat_service.py)     │
POST /chat/chatting/   (body: session_id, question)               │
   │                     │   └─ "chatting" ─► AgentChatService    │  LangGraph
   ▼                     │                  (agent/service.py)    │
chat/views.py  send() ───┼──►               uses ChatService      │
                         │                  underneath            │
                         └────────────────────────────────────────┘
```

- There is **one chat service**, `ChatService`. It does Normal RAG and saves every turn.
- The **agent runtime** (`chat/services/agent/`) sits on top of it. It runs the agent's graph with LangGraph and uses `ChatService` to save the turn.
- **The URL picks who answers:**

| URL the browser calls | Who answers |
|---|---|
| `POST /chat/` | Normal RAG (`ChatService`) |
| `POST /chat/chat_agent/` | The agent whose slug is `/chat/chat_agent/` |
| `POST /chat/chat2/` | The agent whose slug is `/chat/chat2/` |
| `POST /chat/<unknown>/` | `404 No agent at '/chat/<unknown>/' for this company.` |

**Normal RAG is hidden in the chat page for now:** the agent dropdown lists
only agents and starts on the first one, so every message goes to an agent.
`POST /chat/` still works on the server.

Which chat the message belongs to is sent in the request body as `session_id`
(with the `question`). A request without `session_id` gets
`400 session_id is required.`

### The slug is the complete route

Each agent's **slug** column (`agents_agent.slug`) stores its complete chat
route, so the request path is matched against it directly:

| Agent | `agents_agent.slug` | Chat sends to |
|---|---|---|
| Chat agent | `/chat/chat_agent/` | `POST /chat/chat_agent/` |
| a new agent | `/chat/chat2/` | `POST /chat/chat2/` |

- **Unique across all agents**, in every company and the global ones
  (constraint `agents_agent_slug_unique`), so one route points to one agent.
- Format: `/chat/<name>/`, where the name is lowercase letters, digits, `-`
  and `_` (max 64), starting with a letter or digit.
- You can type just the name (`chat2`) or `/chat/chat2`: the API, Django admin
  and the agent builder save it as `/chat/chat2/`.
- Reserved names (they are other chat URLs): `new`, `agents-json`,
  `sessions-json`, `models-json`, `companies-json`.
- The name part (`chat_agent`) is still used where a `/` would not fit: the
  agent's Langfuse prompt name and the graph JSON's `"agent"` value
  (`Agent.slug_name`).
- Migrations: `agents/0020_agent_slug_unique` (one slug per agent across all
  companies) and `agents/0021_agent_slug_full_route` (turned `chat_agent` into
  `/chat/chat_agent/`).

---

## 1. Where the code is

```
chat/
├── urls.py                    POST /chat/ and POST /chat/<name>/ (= an agent's slug)
├── views.py                   send(): reads the request, picks the path, streams the reply
├── templates/chat/chat_page.html
│                              dropdown, builds the send URL, logs to the browser console
└── services/
    ├── chat_service.py        ChatService: Normal RAG + create_turn() (saves turns)
    ├── router.py              find_agent(), service_for(), chat_service()
    ├── agent_capabilities.py  preflight(): can this agent handle this request?
    ├── file_commands.py       "process / stop / resume / status" file commands
    ├── session_files.py       the chat's attached files
    └── agent/                 the LangGraph runtime
        ├── service.py         AgentChatService: runs one turn, streams it, saves it
        ├── graph.py           graph JSON → compiled LangGraph graph (cached)
        ├── tools.py           the tools an agent can call
        ├── prompts.py         system prompt for each model node
        ├── context.py         AgentContext: per-run data every node/tool gets
        ├── checkpointer.py    Postgres memory (one thread per chat session)
        └── studio.py          only for `langgraph dev` / LangGraph Studio

agents/
├── models.py                  Agent (has the `graph` JSON field), Step, Tool
├── services/graph_json.py     Steps → graph JSON
├── services/agent_prompts.py  agent role + model config, from Langfuse
└── signals.py                 rewrites Agent.graph whenever steps/tools change
```

---

## 2. Before chatting: how an agent's graph is made

```
Agent builder (canvas)  ──save steps──►  agents/signals.py  ──►  graph_json.sync_agent_graph()
                                                                     │
                                                                     ▼
                                                     Agent.graph  (JSON column in the DB)
```

1. In `/agent-builder/` you create an Agent, add Steps, connect START → step → END, and tick tools on each step.
2. The builder saves Steps (`/api/v1/agents/step/`).
3. Whenever a Step, a Step's tools or a Tool changes, `agents/signals.py` calls
   `sync_agent_graph(agent)` (`agents/services/graph_json.py`). It rewrites the `Agent.graph` JSON.
4. **Chat only ever reads `Agent.graph`; it never reads Steps.**

The agent's **role text** and **model config** (provider, model, temperature,
max tokens, retrieval top_k) live in Langfuse, in a prompt named after the
name part of the agent's slug, e.g. `chat_agent` for `/chat/chat_agent/`
(`agents/services/agent_prompts.py`).

### The graph JSON

Example for `chat_agent` (one step called `main`; tool list shortened):

```json
{
  "version": 1,
  "agent": "chat_agent",
  "nodes": [
    {"id": "__start__", "type": "start"},
    {"id": "main", "type": "model", "name": "Main step",
     "tools": ["chat_summarise", "create_chart", "document_search", "web_search"]},
    {"id": "main__tools", "type": "tools", "for": "main"},
    {"id": "__end__", "type": "end"}
  ],
  "edges": [
    {"source": "__start__", "target": "main"},
    {"source": "main", "target": "main__tools", "condition": "tools"},
    {"source": "main", "target": "__end__", "condition": "next"},
    {"source": "main__tools", "target": "main"}
  ]
}
```

| Part | Meaning |
|---|---|
| `model` node | Calls the LLM. Its `tools` are the tools the LLM may call. |
| `tools` node | Runs the tools the LLM asked for. `for` names its model node. |
| edge `"condition": "tools"` | Taken when the LLM asks for a tool. |
| edge `"condition": "next"` | Taken when the LLM answers (go to the next step, or END). |
| edge without `condition` | Always taken. |

A step with no tools (or only `general_assistant`) is one `model` node with no `tools` node.
`{}` means the agent has no runnable flow; chat then tells the user to pick another agent.

You can see the JSON in `GET /api/v1/agents/agent/{id}/` (field `graph`) and
`GET /api/v1/agents/agent/load/` (key `graph`).

---

## 3. One message, step by step

### Step 1: the browser picks the URL (`chat_page.html`)

- `loadAgents()` fills the agent dropdown from `GET /chat/agents-json/`.
  Each option keeps the agent's id (`value`) and slug, its complete route (`data-slug`).
  The first option, "Normal RAG", has no slug.
- When the user sends, `postMessage()` calls `sendUrl()`:
  - no agent selected → `POST /chat/`
  - agent selected → `POST` to the agent's slug, e.g. `/chat/chat_agent/`
- The body carries `session_id` (the open chat), `question`, and any
  attachments / picked files.
- It logs the request to the browser console:
  ```
  [chat] send -> agent=chatting url=/chat/chatting/ chat=01M7...
  ```
- The request asks for a stream (`Accept: text/event-stream`), so the answer arrives piece by piece.

### Step 2: Django routes it (`chat/urls.py`)

```python
path("", views.chat_root, name="home"),                # GET page, POST = Normal RAG
path("<str:key>/", views.session_or_agent, name="session"),
# GET /chat/<session id>/ opens that chat; POST /chat/<name>/ sends to the agent with that slug
```

The method decides: a **GET** shows the chat page (or opens a chat by its id), a
**POST** sends a message, and `send()` gets the request path
(`/chat/chat_agent/`) as `agent_slug`.
Because `/chat/new/`, `/chat/agents-json/`, `/chat/sessions-json/`,
`/chat/models-json/` and `/chat/companies-json/` already exist, those names
are reserved and can't be agent names.

### Step 3: the view reads the request (`chat/views.py` → `send()` / `_send()`)

`send()` is a thin wrapper: it reads `session_id` from the body (400 when it is
missing), calls `_send()` and adds two response headers that the browser
console prints (see step 10). `_send()` does the work:

1. Loads the chat session (`_resolve_session`) for the signed-in user.
2. Reads the question, attachments (`attachment_ids`) and files ticked in the
   picker (`selected_file_ids`), and checks they belong to this chat.
3. **Finds the agent from the URL:** `router.find_agent(session.company, agent_slug)`
   finds the agent whose slug equals the request path (`/chat/chat_agent/`).
   - Slugs are unique, so this is one agent; it must belong to the chat's company or be global.
   - Not found → returns **404**.
   - No slug → `selected_agent = None` (Normal RAG).
4. **Remembers the choice:** if the session's saved agent is different, it saves
   `session.selected_agent`. This is only so the dropdown is pre-filled when
   the chat is reopened. **The URL decides who answers, not this field.**

### Step 4: can the agent handle it? (`agent_capabilities.preflight()`)

Only when an agent is in the URL. This makes no LLM call and writes nothing.

1. `classify_intent()` sorts the message with fixed word patterns:
   process files, stop/resume, status, list files, summary, document question,
   web question, or none.
2. It reads which tools the agent's graph JSON binds (`enabled_tool_rows(agent)`).
3. It returns one of three outcomes:

| Outcome | When | What happens next |
|---|---|---|
| `pipeline` | The agent has the tool the request needs (or no special tool is needed) | The **agent** answers (step 7b) |
| `fallback` | A read request (summary, document, web, status) but the agent lacks that tool | **Normal RAG** answers (step 7a); the turn records why |
| `blocked` | An action (process / stop / resume files) the agent lacks the tool for, or the agent has no runnable flow | A fixed refusal is saved; no LLM runs |

### Step 5: short replies that skip the LLM (`views.py`)

Some messages are answered without any LLM:

- **Normal RAG or fallback** → `_file_command_reply()` handles file commands
  ("process a.csv", "stop", "status"), a file sent with no message
  ("what should I do with it?") and an unclear chart target.
- **Agent pipeline** → `_pipeline_direct_reply()` handles only the clarifying
  questions. File commands are not handled here; they go to the agent's own
  tools, so the agent's tool limits apply.

If one of these replies, the request ends here.

### Step 6: the router builds the service (`chat/services/router.py`)

Inside `_run_turn()` in the view:

```python
if run_pipeline:                       # outcome == "pipeline"
    service = router.service_for(selected_agent, llm_client)   # AgentChatService
else:
    service = router.chat_service(llm_client)                  # ChatService (Normal RAG)
service.stream_message(...)
```

Both services have the same methods, `stream_message()` and `process_message()`,
so the view does not care which one it got.

### Step 7a: Normal RAG path (`ChatService.stream_message()`)

1. `_prepare_turn()`: loads earlier turns, searches the knowledge base and the
   chat's files for passages (`RAG_TOP_K`), and builds the prompt with `[n]` citations.
2. `_generate_completion()`: calls the LLM once and streams the text.
   Chart requests ask for a structured reply (chart + caption) instead.
3. `_finalize_turn()`: saves the turn with `create_turn()` (answer, citations, chart, metadata).
4. Yields `("delta", text)` pieces, then `("done", turn)`.

No LangGraph is used on this path.

### Step 7b: Agent path (`AgentChatService.stream_message()` in `agent/service.py`)

1. **Checks** (`_check_turn`): signed-in user, the session belongs to them, and
   the agent belongs to the chat's company (or is global).
2. **Role and model** (`_runtime`): reads the agent's role text and config from
   Langfuse (`get_role_and_config`) and builds the LLM client with that model.
   The agent's model wins over the model picker in the chat header.
3. **Context** (`AgentContext`): user, company, session, agent, LLM, role,
   the chat's file list, the message, attachments, picked files and the
   agent's tools (`enabled_tool_rows`). Every node and tool receives it.
4. **Graph** (`graph.get_graph(agent.graph)`): compiles the JSON into a LangGraph
   `StateGraph` with the Postgres checkpointer. Compiled graphs are cached per
   JSON, so a graph compiles once until the agent changes.
5. **Memory** (`_resume_point`): the chat session is a LangGraph thread
   (`thread_id` = session id). The run continues from the `checkpoint_id` of the
   last saved agent turn. Turns saved since without the agent (Normal RAG,
   refusals) are added as input. With no earlier agent turn, the thread starts
   over with the last `CHAT_RECENT_TURNS` turns.
6. **Run** (`graph.stream(...)`), see section 4 below.
7. **Stream events** (`_events`):
   - answer tokens of the last step → `("delta", text)`
   - each tool call, tool result and earlier step's reply → `("trace", item)`
     (shown under the answer as **Agent steps**)
8. **Save** (`_persist`): takes the last AI message as the answer, keeps only
   the citations the answer actually uses (`[n]`), and the chart a tool made. It saves
   the turn through `ChatService.create_turn()` with metadata:
   `source: "langgraph"`, `tools_used`, `trace`, `flow`, `checkpoint_id`,
   `agent_selection`.
9. Yields `("done", turn)`.

If the browser disconnects mid-answer, both paths still finish and save the turn.

### Step 8: the view streams it back (`_send_events()`)

Each event becomes one Server-Sent Event line:

```
data: {"delta": "The total is"}
data: {"trace": {"kind": "tool_call", "tool": "document_search", ...}}
data: {"done": true, "user": {...}, "assistant": {...}, "session": {...}}
```

Errors become `data: {"error": "...", "status": 502}`
(LLM not configured → 503, LLM / agent failure → 502, retrieval failure → 502).

### Step 9: the browser shows it (`readSendStream()` in `chat_page.html`)

`delta` text is appended live, `trace` lines appear under **Agent steps**, and
`done` renders the final message (citations, chart).

### Step 10: the browser console says who answered

`send()` adds two headers to every reply, and the page logs them:

```
[chat] send -> agent=chatting url=/chat/chatting/ chat=01M7...
[chat] answered by agent=chatting route=pipeline (HTTP 200)
```

| `X-Chat-Agent` | `X-Chat-Route` | Meaning |
|---|---|---|
| `normal-rag` | `rag` | No agent in the URL; Normal RAG answered |
| `/chat/<name>/` | `pipeline` | The agent ran its LangGraph graph |
| `/chat/<name>/` | `fallback` | The agent lacked the tool for this read request; Normal RAG answered |
| `/chat/<name>/` | `blocked` | The agent refused (missing action tool or no runnable flow) |

You can also see both headers in DevTools → Network → the `send` request → Response Headers.

---

## 4. Inside the graph run

```
user message
   │
   ▼
main (LLM) ──asks for a tool──► main__tools (runs it) ──result──► main (LLM)
   │
   └──gives answer──► END ──► saved and shown in chat
```

- A **model node** (`graph._model_node`) builds the system prompt (`prompts.system_prompt`:
  agent role + real model name + formatting and tool rules + the chat's file list),
  binds the step's tools (`tools.tools_for`) and calls the LLM with the recent messages.
- `_route` sends the flow to the **tools node** when the reply asks for a tool,
  otherwise along the `next` edge.
- A **tools node** (`graph._tool_node`) runs the requested tools with LangGraph's
  `ToolNode` and goes back to its model node, which sees the results.
- This loop can repeat up to `AGENT_RECURSION_LIMIT` (default 12) graph steps.
- With several steps, they run one after another. The last step's reply is the
  answer; earlier steps' replies show under **Agent steps**.

### Tools

| Tool | What it does |
|---|---|
| `document_search` | Searches the knowledge base and chat files; the answer cites `[n]` passages. |
| `create_chart` | Shows a pie, bar or line chart. |
| `chart_from_file` | Builds a chart from a CSV file. |
| `chat_summarise` | Gives the LLM the chat transcript to summarise. |
| `web_search` | No live web access yet; the LLM answers from its own knowledge with a note. |
| `add_to_knowledge_base` | Processes chat files into the knowledge base (start, stop, resume, cancel, list). |
| `document_processing_status` | Reports file processing status (same actions). |
| `general_assistant` | Not a real tool: marks a step that answers directly. |
| Your own tool | Runs one LLM call using the tool's description. |

The LLM reads each tool's description to decide whether to call it. Edit
descriptions in Django admin (Agents → Tools) or with
`PATCH /api/v1/agents/tool/{id}/`; the defaults are in `agents/services/tool_docs.py`.

### Memory between messages

- Each chat session is one LangGraph **thread** (`thread_id` = the session id).
- LangGraph saves it in Postgres (tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`).
- Each saved agent turn stores its `checkpoint_id`; the next message continues
  from there, so a failed run never leaves junk in the conversation.

---

## 5. Other things that use the session's saved agent

These buttons act on files without a chat message, so there is no send URL.
They use the agent saved on the session (the last one used to send, or picked
in the dropdown), via `_require_selected_agent_capability()` in `views.py`:

- Process from the file panel (`/chat/<id>/panel-process/`)
- Retry processing (`/chat/<id>/reprocess/`)
- File actions (`/chat/<id>/files/<entry>/<action>/`)

If that agent lacks the needed tool, they answer 403.

Changing the dropdown calls `POST /chat/<id>/selected-agent/` to save the choice
right away; creating a chat (`POST /chat/new/`) can save one too.

---

## 6. How to try it

1. Open the chat, pick **Chat agent** in the agent dropdown, send "hello".
2. Open DevTools → Console. You should see:
   ```
   [chat] send -> agent=/chat/chat_agent/ url=/chat/chat_agent/ chat=<id>
   [chat] answered by agent=/chat/chat_agent/ route=pipeline (HTTP 200)
   ```
3. In DevTools → Network, the request is `POST /chat/chat_agent/` and its
   Payload has `session_id` and `question`.
4. Pick **Normal RAG**: the URL is `POST /chat/` and the console says `agent=normal-rag route=rag`.
5. In the database, the saved turn's `metadata.agent_selection.agent_slug` names
   the agent, and `metadata.source` is `"langgraph"` for agent answers.

To add a new agent with its own endpoint (for example `chat2`): create it in
the agent builder with slug `chat2` (the dialog shows the saved slug
`/chat/chat2/`), connect START → a step → END and tick its tools. It appears in the
dropdown, and its messages go to `POST /chat/chat2/`.

## 7. Optional: LangGraph Studio

`langgraph.json` points at `chat/services/agent/studio.py`, which compiles the
global `chat_agent`'s graph so you can view it with `langgraph dev`. It is for
looking at the graph's structure only; real chat turns need a session and user.
