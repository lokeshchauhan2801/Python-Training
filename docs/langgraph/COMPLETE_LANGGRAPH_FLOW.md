# How an Agent runs (LangGraph flow)

## The idea in one line

You draw the flow in the builder → it is saved as **graph JSON on the Agent** → chat reads that JSON and runs it with LangGraph.

```
Builder (steps)  ──save──►  Agent.graph (JSON in DB)  ──load──►  LangGraph  ──►  answer in chat
```

## 1. Building the flow (Agent builder)

- In `/agent-builder/` you create an Agent, add Steps, connect them START → step → END, and tick tools on each step.
- The builder saves Steps as before (`/api/v1/agents/step/`).
- Whenever a step, a step's tools or a tool changes, the backend rewrites the Agent's `graph` column.
  - Code: `agents/services/graph_json.py` (builds the JSON) and `agents/signals.py` (runs it on every change).

## 2. The graph JSON (stored in `agents_agent.graph`)

Example for the default `chat_agent` (one step called `main`):

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

What the parts mean:

| Part | Meaning |
|---|---|
| `model` node | Calls the LLM. Its `tools` are the tools the LLM is allowed to call. |
| `tools` node | Runs the tools the LLM asked for. `for` says which model node it belongs to. |
| edge with `"condition": "tools"` | Taken when the LLM asks for a tool. |
| edge with `"condition": "next"` | Taken when the LLM gives its answer (go to the next step or END). |
| edge without `condition` | Always taken. |

A step with no tools (or only `general_assistant`) is just one `model` node, with no `tools` node.

`{}` means the agent has no runnable flow (no step connected from START); chat then tells the user to pick another agent.

You can see the JSON in the database, in `GET /api/v1/agents/agent/{id}/` (field `graph`) and in `GET /api/v1/agents/agent/load/` (key `graph`).

## 3. Running a chat message

```
user message
   │
   ▼
main (LLM) ──asks for a tool──► main__tools (runs it) ──result──► main (LLM)
   │
   └──gives answer──► END ──► saved and shown in chat
```

Step by step:

1. The user picks an Agent in the chat header and sends a message (`POST /chat/{session_id}/send/`).
2. `chat/agent/service.py` reads `agent.graph` (it never reads Steps).
3. `chat/agent/graph.py` turns the JSON into a LangGraph graph (cached in memory until the JSON changes).
4. The LLM runs. If it asks for tools, they run, and the LLM sees the results. This can repeat a few times (`AGENT_RECURSION_LIMIT`, default 12).
5. When the LLM answers without asking for a tool, the flow goes to the next step or END.
6. The answer streams to the chat. Each tool call shows under the answer as **Agent steps**.
7. The turn is saved with its tools used, the trace and a `checkpoint_id`.

With several steps, they run one after another and the last step's reply is the answer.

## 4. Memory between messages

- Each chat session is one LangGraph **thread** (`thread_id` = the session id).
- LangGraph saves the conversation in Postgres (tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`).
- Each saved turn stores its `checkpoint_id`; the next message continues from there, so a failed run never leaves junk in the conversation.

## 5. Tools

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

Tool descriptions (what the LLM reads to decide) are editable per tool in the builder.

## 6. Where the code is

| File | Job |
|---|---|
| `agents/services/graph_json.py` | Steps → graph JSON |
| `agents/signals.py` | Rewrites `Agent.graph` when steps or tools change |
| `chat/agent/graph.py` | Graph JSON → LangGraph graph |
| `chat/agent/service.py` | Runs one chat turn, streams it, saves it |
| `chat/agent/tools.py` | The tools |
| `chat/agent/prompts.py` | System prompt (agent role from Langfuse + rules) |
| `chat/agent/checkpointer.py` | Postgres memory |
| `chat/services/agent_capabilities.py` | Checks the agent has the tool a request needs before running |
| `langgraph.json`, `chat/agent/studio.py` | Optional: open the graph in LangGraph Studio |

## 7. Normal RAG

If no Agent is selected, chat uses Normal RAG (`chat/services/chat_service.py`), which does not use LangGraph.
