# Chat and agent selection: how a message is answered

This is the current flow of the chat page: how an agent is chosen, how a
message reaches it, and what the agent does with it. The same flow as
diagrams: [CHAT_AND_AGENT_FLOW_DIAGRAMS.md](CHAT_AND_AGENT_FLOW_DIAGRAMS.md).

> **Normal RAG is hidden in the chat page for now.** The agent dropdown lists
> only agents, so every message is answered by an agent. The Normal RAG
> endpoint (`POST /chat/`) still works on the server.

---

## 1. The picture

```
Chat page (chat_page.html)
  dropdown: "chat agent 2"  → its slug "/chat/chat-agent-2/"
  │
  │  POST /chat/chat-agent-2/        body: session_id, question, attachment_ids
  ▼
chat/urls.py   "<str:key>/"  →  views.session_or_agent   (POST → send)
  ▼
chat/views.py  send() / _send()
  1. load the chat (session_id)
  2. router.find_agent(company, request path)      ← the URL picks the agent
  3. remember it on the chat (dropdown pre-fill)
  4. agent_capabilities.preflight()                 → pipeline / fallback / blocked
  5. router.service_for(agent)                      → AgentChatService
  ▼
chat/services/agent/service.py   AgentChatService.stream_message()
  role + model (Langfuse) → files for this message → graph (Agent.graph)
  → LangGraph run (model ⇄ tools) → stream answer → save the turn
  ▼
Chat page shows the answer, Agent steps, and any chart
```

---

## 2. Where things live

| What | Where |
|---|---|
| Agent table, slug = complete chat route | `agents/models.py` (`Agent.slug`, e.g. `/chat/chat_agent/`) |
| Agent's role + model config | Langfuse prompt named after the slug's name part (`chat_agent`), `agents/services/agent_prompts.py` |
| Agent's flow (steps + tools) | `Agent.graph` JSON, rebuilt from the builder's steps by `agents/services/graph_json.py` |
| The chat's saved agent | `chat_conversation_session.selected_agent_id` (`chat/models.py`) |
| Chat page (dropdown, send, stream) | `chat/templates/chat/chat_page.html` |
| Routes | `chat/urls.py` |
| Request handling | `chat/views.py` (`send`, `_send`, `selected_agent`, `agents_json`) |
| Agent or RAG? | `chat/services/router.py` |
| Can this agent do it? | `chat/services/agent_capabilities.py` |
| Agent runtime | `chat/services/agent/` (`service.py`, `graph.py`, `tools.py`, `prompts.py`, `context.py`, `checkpointer.py`) |
| Normal RAG (used by agents' tools, hidden in the UI) | `chat/services/chat_service.py` |

---

## 3. The agent's slug is its route

Each agent's `slug` column holds its **complete chat route**:

| Agent | `agents_agent.slug` | Messages go to |
|---|---|---|
| Chat agent | `/chat/chat_agent/` | `POST /chat/chat_agent/` |
| chat agent 2 | `/chat/chat-agent-2/` | `POST /chat/chat-agent-2/` |

- Unique across all agents (every company and the global ones).
- Format `/chat/<name>/`; the name is lowercase letters, digits, `-`, `_`.
  Typing `chat2` or `/chat/chat2` in the builder, admin or API saves `/chat/chat2/`.
- Reserved names (other chat URLs): `new`, `agents-json`, `sessions-json`,
  `models-json`, `companies-json`.
- The name part (`chat_agent`) is the Langfuse prompt name (`Agent.slug_name`).
- A permanently deleted agent takes its Langfuse prompt with it
  (`agents/signals.py`); a soft delete keeps it.

`GET /chat/<session id>/` still opens a chat: the request **method** tells the
two apart (GET = open a chat, POST = send to an agent).

---

## 4. Choosing an agent in the page

1. **Load the list.** `loadAgents()` calls `GET /chat/agents-json/?company=<id>`
   (`views.agents_json`): the company's agents plus the global ones, each with
   `id`, `name` and `slug`. Each dropdown option keeps the id and the slug
   (`data-slug`).
2. **Pick the default.** The chat's saved agent if it is still there, else the
   first agent. With no agents the dropdown says "No agent available" and
   sending is blocked.
3. **Change it.** Choosing another agent calls
   `POST /chat/<session id>/selected-agent/` with `{"agent_id": ...}`
   (`views.selected_agent`), which saves `selected_agent_id` on the chat.
   A new chat (`POST /chat/new/`) is created with the chosen agent.

The saved agent is only a **memory** for the dropdown and for the file panel
buttons (process / retry / file actions check that agent's tools). **The send
URL decides who answers.**

---

## 5. Sending a message

### In the browser (`postMessage()` in `chat_page.html`)

- URL: `sendUrl()` = the selected agent's slug, e.g. `/chat/chat-agent-2/`.
- Body: `session_id`, `question`, `attachment_ids` (files attached now),
  `selected_file_ids` (files ticked in a picker), optional `model_name`/`provider`.
- Header `Accept: text/event-stream`, so the answer streams.
- The console logs the request and who answered:
  ```
  [chat] send -> agent=/chat/chat-agent-2/ url=/chat/chat-agent-2/ chat=01M7...
  [chat] answered by agent=/chat/chat-agent-2/ route=pipeline (HTTP 200)
  ```

### On the server (`chat/views.py`)

`session_or_agent()` sends a POST to `send(request, agent_slug=request.path_info)`.

`send()`:
- reads `session_id` from the body (`400 session_id is required.` without it),
- calls `_send()`,
- adds response headers `X-Chat-Agent` (the slug) and `X-Chat-Route`
  (`pipeline`, `fallback`, `blocked` or `rag`); the console prints them.

`_send()`:
1. Loads the chat for the signed-in user and checks the attachments and
   picked files belong to it.
2. Needs a question, an attachment or a picked file.
3. **Finds the agent:** `router.find_agent(company, "/chat/chat-agent-2/")` -
   the agent with that slug, of this company or global. None → `404 No agent at '...'`.
4. **Remembers it:** saves `session.selected_agent` if it changed.
5. **Capability check** (`agent_capabilities.preflight`, no LLM, no writes):
   classifies the message (process files, stop/resume, status, list, summary,
   document question, web question, other) and compares it with the tools the
   agent's graph binds.

   | Outcome | When | Then |
   |---|---|---|
   | `pipeline` | The agent can do it | The agent answers |
   | `fallback` | A read request needing a tool the agent lacks (e.g. web search) | Normal RAG answers; the turn records why |
   | `blocked` | An action it cannot do (process/stop files) or no runnable flow | A fixed refusal is saved, no LLM |

   A document question about a file attached to the message, or a chat file it
   refers to, is always `pipeline`: the agent gets that file's content itself.
6. **Before the graph:** only a file sent with no message gets a fixed reply
   (it asks what to do with the file). Everything else, including which file
   "this country file" means, is left to the agent.
7. **Runs the turn:** `router.service_for(agent)` builds `AgentChatService`
   and calls `stream_message(...)`.

---

## 6. Inside the agent (`chat/services/agent/service.py`)

`AgentChatService.stream_message()`:

1. **Checks** the user owns the chat and the agent belongs to its company (or is global).
2. **Role and model** from the agent's Langfuse prompt (`get_role_and_config`):
   role text, provider, model, temperature, max tokens, passages per question.
   The agent's model wins over the model picker in the header.
3. **Files for this message** (`_message_files`), given to the model directly,
   no tool needed:
   - the files attached to the message; else
   - when the message refers to a chat file: the file whose name matches, the
     matching candidates, or the newest file ("this file" = the latest);
   - a CSV comes with its rows **and exact counts of each value**
     (`Country: England 43, Scotland 32, ...`).
4. **Context** (`AgentContext`): user, company, chat, agent, model, role, the
   chat's file list, the message files, the agent's tools.
5. **Graph:** `graph.get_graph(agent.graph)` compiles the JSON into a LangGraph
   graph (cached until the JSON changes).
6. **Memory:** one LangGraph thread per **(chat, agent)**:
   `thread_id = "<session id>:<agent id>"`, stored in Postgres.
   - The agent continues from its own last turn (`metadata.checkpoint_id` +
     `metadata.thread_id`), so it remembers its own tool calls.
   - Turns answered by other agents (or Normal RAG) since then are added as
     **one quoted context block**, so the agent never copies another agent's
     replies or tools.
7. **Run:** `graph.stream(...)`. A model node calls the LLM with its step's
   tools; if it asks for tools, the tools node runs them and the model sees
   the results; this repeats up to `AGENT_RECURSION_LIMIT` (12).
8. **Stream:** answer tokens → `delta` events; tool calls/results → `trace`
   events (shown as **Agent steps**). A ```` ```chart ```` block is never streamed.
9. **Save** (`_persist`) through `ChatService.create_turn()`: the answer, the
   citations it uses, the chart, and metadata `source: "langgraph"`,
   `tools_used`, `trace`, `flow`, `checkpoint_id`, `thread_id`,
   `agent_selection` (agent id, slug, outcome).

### The system prompt (`chat/services/agent/prompts.py`)

Built per model call, in this order:
1. The agent's role (Langfuse).
2. The real provider and model name.
3. Plain-text formatting rule.
4. Tool rules (only when the step has tools): call only the tools you have
   now; earlier messages may mention others.
5. Chart rule:
   - step has `create_chart` → draw charts with the tool;
   - no `create_chart` → write a ```` ```chart ```` JSON block; the app draws it.
6. Chart data rule: a chart request with no topic, no file and nothing attached
   (and not a follow-up) → **ask which file** (listing the chat's files) or what
   data; never sample or invented numbers.
7. Task rule: finish the job; don't offer options when the request is clear.
8. The chat's file list, then the message files' content.

---

## 7. Charts

| How | When | What the app does |
|---|---|---|
| `create_chart` tool | The step has it | The tool's result is the chart |
| `chart_from_file` tool | The step has it and the user chose a file (attached, named, or a follow-up) | Counts the CSV's values exactly; with no file chosen it tells the model to ask |
| ```` ```chart ```` JSON block in the reply | The step has no `create_chart` | `chart_from_reply()` validates it (`ChartData`), saves it as the chart, removes the JSON from the text |

The page draws the saved chart (`chart_data`) with **View large** / **Download chart**.

Examples (tested with real messages):

| Message | What happens |
|---|---|
| "create a pie chart" (files in the chat, none attached) | Asks which file, listing them |
| "create a pie chart" + CSV attached | Chart of that file, exact counts |
| "create pie chart from this country file" | Uses the newest file (`county_uk.csv`) |
| "make it a bar chart" | Same data as a bar chart |
| "pie chart of the top 5 economies" | General knowledge, title says "(general knowledge)" |
| "chart the sales file" (no such file) | Says there is no sales file and lists the files |

---

## 8. What the page receives

The send reply is a stream of Server-Sent Events:

```
data: {"delta": "Pie chart of the 94 counties..."}
data: {"trace": {"kind": "tool_call", "tool": "create_chart", ...}}
data: {"done": true, "user": {...}, "assistant": {..., "chart_data": {...}}, "session": {...}}
```

Errors arrive as `data: {"error": "...", "status": 502}` (LLM not configured 503,
LLM/agent failure 502). If the browser disconnects, the turn is still finished
and saved.

---

## 9. Checking it yourself

1. Open the chat, pick an agent, send a message.
2. DevTools → Network → the request is `POST /chat/<slug>/`; Payload has
   `session_id`; Response Headers have `X-Chat-Agent` and `X-Chat-Route`.
3. DevTools → Console shows the two `[chat]` lines.
4. In the database, the turn's `metadata.agent_selection.agent_slug` names the
   agent and `metadata.thread_id` is `<session id>:<agent id>`.

---

## 10. Changing an agent

- Steps and tools: the agent builder (`/agent-builder/`); every change rebuilds `Agent.graph`.
- Role and model: the builder's **Save role** / **Save config** (stored in Langfuse).
- A step without `create_chart` still draws charts (JSON block); a step
  without `document_search` still reads files attached to or named in the message.
