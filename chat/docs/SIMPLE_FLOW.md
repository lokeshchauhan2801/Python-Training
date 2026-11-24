# Chat with agents: the simple version

How a chat message is answered by an agent, in plain terms. For every detail
see [CHAT_AND_AGENT_FLOW.md](CHAT_AND_AGENT_FLOW.md); for diagrams see
[CHAT_AND_AGENT_FLOW_DIAGRAMS.md](CHAT_AND_AGENT_FLOW_DIAGRAMS.md).

---

## The idea in three lines

1. Each agent is one row in the database (`agents_agent`). Its **slug** is its
   address, e.g. `/chat/chat-agent-2/`, and its **graph** column holds its flow as JSON.
2. The chat page sends each message to the selected agent's address.
3. The backend finds the agent row by that address, turns its JSON into a
   LangGraph graph, and runs it. Same code for every agent: only the row changes.

---

## 1. An agent in the database

You build the agent in the agent builder (steps + tools). On save, the whole
flow is written into one column, `agents_agent.graph`. Example, **chat agent 2**
(slug `/chat/chat-agent-2/`):

```json
{
  "agent": "chat-agent-2",
  "nodes": [
    {"id": "__start__", "type": "start"},
    {"id": "task", "name": "Task", "type": "model",
     "tools": ["add_to_knowledge_base", "chat_summarise", "create_chart",
               "document_processing_status", "document_search"]},
    {"id": "task__tools", "type": "tools", "for": "task"},
    {"id": "__end__", "type": "end"}
  ],
  "edges": [
    {"source": "__start__",   "target": "task"},
    {"source": "task",        "target": "task__tools", "condition": "tools"},
    {"source": "task",        "target": "__end__",     "condition": "next"},
    {"source": "task__tools", "target": "task"}
  ]
}
```

In words:

```
START → task (the LLM, allowed these 5 tools)
          ├─ LLM asks for a tool → task__tools runs it → back to task
          └─ LLM answers         → END
```

The agent's **role text and model** (deepseek-chat, temperature, ...) are not
in this JSON: they are in Langfuse, in a prompt named `chat-agent-2`.

---

## 2. How one backend serves every agent

There is **one** route for all agents (`chat/urls.py`):

```python
path("<str:key>/", views.session_or_agent)
```

- `<str:key>` is a wildcard: `/chat/chat_agent/`, `/chat/chat-agent-2/`,
  `/chat/anything/` all reach the same function.
- **GET** `/chat/<session id>/` opens a chat; **POST** `/chat/<slug>/` sends a message.
- The backend looks the agent up by the address, like finding a user by email:

```python
Agent.objects.filter(slug="/chat/chat-agent-2/", company=<yours or global>).first()
```

That row brings everything that makes the agent different:

| From the row | chat_agent | chat agent 2 |
|---|---|---|
| Address (slug) | `/chat/chat_agent/` | `/chat/chat-agent-2/` |
| Flow (graph JSON) | step `main` | step `task` |
| Tools | 6 (incl. `chart_from_file`, `web_search`) | 5 |
| Role + model (Langfuse) | prompt `chat_agent` | prompt `chat-agent-2` |
| Memory per chat | `<chat id>:<chat_agent id>` | `<chat id>:<chat agent 2 id>` |

**A new agent needs no code change:** create `chat3` in the builder, it is
saved as `/chat/chat3/`, appears in the dropdown, and its messages go to
`POST /chat/chat3/` through the same route.

---

## 3. Example: the user sends "hi" to chat agent 2

| # | What happens | Where |
|---|---|---|
| 1 | The page sends `POST /chat/chat-agent-2/` with `session_id` and `question=hi` | `chat_page.html` |
| 2 | Load the chat (DB: `chat_conversation_session`) | `views._send` |
| 3 | **Load the agent row by its slug; the `graph` JSON comes with it** (DB: `agents_agent`) | `router.find_agent` |
| 4 | Remember the agent on the chat for the dropdown (DB write, only if it changed) | `views._send` |
| 5 | Check the agent can do it: "hi" needs no special tool → the agent answers | `agent_capabilities.preflight` |
| 6 | Get the role and model (Langfuse prompt `chat-agent-2`) | `AgentChatService._runtime` |
| 7 | Get the tools named in the JSON (DB: `agents_tool`) | `tools.enabled_tool_rows` |
| 8 | Files for this message: none ("hi" names no file) | `_message_files` |
| 9 | Turn the JSON into a LangGraph graph (kept in memory for the next message) | `graph.get_graph` |
| 10 | Load this agent's memory for this chat (DB: LangGraph `checkpoints`) | `_resume_point` |
| 11 | Run: `START → task`: the LLM answers "Hi! What can I help you with?" without a tool → `END` | `graph.stream` |
| 12 | Stream the answer to the page; save the turn and the memory (DB writes) | `_events`, `_persist` |

The page shows the answer with the label "Agent (no tool)". The JSON is read
from the database **once**, in step 3; everything after uses that copy.

---

## 4. Same agent, a chart: "create a pie chart of county_uk.csv"

Same 12 steps, with three differences:

- **Step 8:** the file's content goes to the model with the message, including
  exact counts (`Country: England 43, Scotland 32, Wales 13, Northern Ireland 6`).
- **Step 11:** the graph goes round the loop once:

  ```
  START → task (LLM asks for create_chart with the numbers)
        → task__tools (create_chart builds the chart)
        → task (LLM writes a short caption) → END
  ```

- **Step 12:** the saved turn also holds the chart; the page draws it and shows
  **Agent steps (2)**.

An agent **without** `create_chart` still draws charts: it writes the chart as
JSON in its reply, and the app turns that into the chart.

---

## 5. Same message, different agent

```
POST /chat/chat_agent/     "chart the web GDP figures"
   → row chat_agent:   has web_search   → the LLM can search the web

POST /chat/chat-agent-2/   "chart the web GDP figures"
   → row chat agent 2: no web_search    → Normal RAG answers (route = fallback)
```

Same code, same chat, same question: a different slug loads a different row,
so the answer is different.

---

## 6. Things to remember

- The **URL decides** which agent answers. The agent saved on the chat only
  pre-fills the dropdown.
- **Unknown address** → `404 No agent at '/chat/...'`. An agent of another
  company is never found.
- Each agent has **its own memory per chat**, so agents never copy each other.
- Change an agent's steps/tools in the builder → its JSON is rebuilt → the next
  message uses the new flow. No restart.
- Normal RAG is hidden in the chat page for now; every message goes to an agent.
