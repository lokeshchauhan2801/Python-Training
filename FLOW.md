# Project Flow — agentic-flow

A map of how a request flows through the codebase, and **how many directories
each piece of functionality touches**. This is the "where does X live and what
does it call" reference; see `README.md` for setup and the feature catalog.

The project is a Django 5.2 + django-unfold app. The admin is the primary UI;
two top-level pages (`/chat/` and `/agent-builder/`) render inside the Unfold
shell; a thin DRF API lives under `api/v1/<app>/`.

---

## Top-level layout

```
agentic-flow/
├─ base/            project config + root URLconf (base/urls.py)
├─ basics/          kernel: ULID, BaseModel, soft-delete, LLMClient, vectorstore, langfuse
├─ auth/            the email-login User
├─ company/         tenant root Company + active-company switcher (company/active.py)
├─ documents/       RAG library: Document/DocumentChunk, extraction, retrieval
├─ chat/            chat thread models + ChatService (RAG pipeline) + /chat/ page
├─ agents/          LangGraph agent builder: models, runtime, tools, API, canvas
├─ scrapper/        SSRF-safe scrape service
├─ infrastructure/  pluggable processing backends (celery/kafka/temporal)
└─ agents/static/agents/canvas/index.html   the React-Flow-style builder canvas
```

---

## Functionality → directory-hop map

Each row lists the files a single request path flows through, **in order**, and
the count of distinct directories it crosses.

### 1. Normal chat turn (no graph configured) — **4 dirs**

```
chat/ → chat/ → documents/ → basics/
```

1. `chat/urls.py` → `chat/views.py::send`
2. `chat/views.py::_orchestrator_for` sees no active graph → `chat/services.py::ChatService.process_message`
3. retrieval: `documents/services.py::find_relevant_chunks`
4. embeddings + vector search: `basics/llm.py` (embed) + `basics/vectorstore.py` (Chroma)

Dirs crossed: `chat`, `documents`, `basics` (3 app dirs; `chat` is entered twice
— view then service).

### 2. Chat turn through the LangGraph flow — **4 dirs**

```
chat/ → chat/services/ → chat/services + documents/ → basics/
```

1. `chat/views.py::send` → `_orchestrator_for(company)`
2. active graph found → `chat/services/langgraph_service.py::LangGraphChatService.process_message`
3. graph build + router + GraphState: all in `chat/services/langgraph_service.py`, which reads `agents/models.py` for the graph DATA model
4. router picks a tool → the slug->handler registry (`get_handler`) and the five tool handlers are defined IN-FILE in `chat/services/langgraph_service.py`
5. the chosen handler calls back into `chat/services/chat_service.py::ChatService` (document_search/chat_summarise) and/or `documents/services.py`, with `basics/llm.py` for the LLM call

Dirs crossed: `chat`, `documents`, `basics`.
The runtime now stays within `chat/` — it hops to `agents/` only for the graph
DATA model (`agents/models.py`). The registry, GraphState, and all five tool
handlers live in the single file `chat/services/langgraph_service.py`.

### 3. Building/editing a flow in the canvas — **3 dirs**

```
agents/ (page) → agents/static/ (canvas) → agents/api/
```

1. `agents/urls.py` → `agents/views.py::agent_builder_page` renders `agents/templates/agents/agent_builder.html`
2. the iframe loads `agents/static/agents/canvas/index.html` (the builder UI)
3. the canvas calls `api/v1/agents/` → `agents/api/urls.py` → `agents/api/views.py` → `agents/api/serializers.py` → `agents/models.py`

Dirs crossed: `agents` (view/template), `agents/static`, `agents/api`. All within
the one `agents/` app.

### 4. Document upload → ready — **3 dirs**

```
documents/ → basics/ → infrastructure/
```

1. `documents/` admin add → `documents/tasks.py::dispatch_document_processing`
2. `documents/services.py::process_document` (extract → embed via `basics/llm.py` → index via `basics/vectorstore.py`)
3. backend dispatch (kafka/temporal/celery) through `infrastructure/`

### 5. Company switch (active company) — **2 dirs**

```
company/ → (every page via context processor)
```

1. header `<select>` POSTs to `company/urls.py` → `company/views.py::set_active_company`
2. `company/active.py::active_company_for_request` resolves it; `company/context_processors.py` exposes it to all templates

---

## The LangGraph flow — where it is and how to drive it

- **Build/edit the flow:** the `/agent-builder/` page (sidebar → Agentic →
  LangGraph). Everything about a flow is done from this one canvas — add STEP
  nodes, wire edges, pick each node's tools, Save, then **Activate graph**.
- **Flow definition (data):** `agents/models.py` — `AgentGraph` (one active per
  company), `AgentNode` (START/STEP/END), `AgentEdge`, `Tool`.
- **Flow runtime (execution):** `chat/services/langgraph_service.py`
  (`LangGraphChatService`), which also holds the GraphState TypedDict, the tool
  registry, and all five tool handlers — one single file. A new chat message for
  a company with an **active** graph runs through it; otherwise chat falls back
  to the normal RAG path.
- **Tools a node can call:** defined IN-FILE in
  `chat/services/langgraph_service.py` — `general_assistant`, `document_search`,
  `chat_summarise`, `web_search` (stub), `prompt_tool`. The registry
  (`register`/`get_handler`/`available_slugs`) lives in the same file.
- **Prompts + tracing:** `basics/langfuse.py` (import-safe; reads
  `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_BASE_URL` from the
  environment, local fallback when absent).
- **Seeding:** `python manage.py seed_agents` creates the four built-in tools +
  a default inactive `START → chat_agent → END` graph per company.

### How the method/tool shows in chat

Each assistant reply shows a **"Method:"** badge at the bottom of the bubble
(`chat/templates/chat/chat_page.html`): the graph tool used when routed through a
flow, else "Document reference" when citations exist, else "General purpose".

### Tool prompts via Langfuse

- `Tool.prompt_name` is a POINTER (a Langfuse prompt NAME), not the prompt text.
  The prompt TEXT is authored and edited in the Langfuse Prompts UI under that
  name; the canvas Tools section edits only the pointer per tool.
- **Prompt-only tools** carry `config = {"source": "langfuse", "kind":
  "prompt_only"}`. They need no registered handler: the runtime routes an
  unregistered-but-prompt-only slug through the generic `prompt_tool` handler
  (in `chat/services/langgraph_service.py`), which answers like the general
  assistant but
  forces the tool's Langfuse prompt as the system message. Create one from the
  canvas ("Add Langfuse tool") — it POSTs `tool/sync_langfuse/` with the prompt
  name (langfuse 4.18.0 has no prompt-listing API, so the client supplies the
  names).
- Every path falls back to the hard-coded prompt when Langfuse is unconfigured:
  `basics/langfuse.py::get_prompt` returns the supplied fallback, so an
  empty/absent managed prompt degrades to today's behaviour.

---

## Route reference

| Route | Entry | Purpose |
|-------|-------|---------|
| `/chat/`, `/chat/<id>/` | `chat/views.py` | the chat page (Unfold shell) |
| `/chat/<id>/send/` | `chat/views.py::send` | run one turn (graph or RAG) |
| `/agent-builder/` | `agents/views.py` | the flow builder canvas (iframe) |
| `/company/set-active-company/` | `company/views.py` | switch active company |
| `api/v1/agents/graph|node|edge|tool/` | `agents/api/views.py` | canvas CRUD + `graph/<id>/load/` + `graph/<id>/activate/` + `tool/sync_langfuse/` |
| `api/v1/chat/...`, `api/v1/documents/...` | each app's `api/` | thin DRF surface |
