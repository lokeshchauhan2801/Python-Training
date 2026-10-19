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

### 2. Chat turn through the LangGraph flow — **5 dirs**

```
chat/ → agents/services/ → agents/tools/ → chat/services + documents/ → basics/
```

1. `chat/views.py::send` → `_orchestrator_for(company)`
2. active graph found → `agents/services/graph_service.py::LangGraphChatService.process_message`
3. graph build + router: `agents/services/graph_service.py` (+ `agents/services/state.py`), reads `agents/models.py`
4. router picks a tool → `agents/tools/__init__.py::get_handler` → one of `agents/tools/*.py`
5. the chosen handler calls back into `chat/services.py::ChatService` (document_search/chat_summarise) and/or `documents/services.py`, with `basics/llm.py` for the LLM call

Dirs crossed: `chat`, `agents/services`, `agents/tools`, `documents`, `basics`.
The graph runtime lives under `agents/` today; the intended target is to move it
into `chat/services/langgraph_service.py` so steps 1–4 stay within `chat/`.

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
- **Flow runtime (execution):** `agents/services/graph_service.py`
  (`LangGraphChatService`) + `agents/services/state.py`. A new chat message for a
  company with an **active** graph runs through it; otherwise chat falls back to
  the normal RAG path.
- **Tools a node can call:** `agents/tools/` — `general_assistant`,
  `document_search`, `chat_summarise`, `web_search` (stub). The registry is
  `agents/tools/base.py` + `agents/tools/__init__.py`.
- **Prompts + tracing:** `basics/langfuse.py` (import-safe; reads
  `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_BASE_URL` from the
  environment, local fallback when absent).
- **Seeding:** `python manage.py seed_agents` creates the four built-in tools +
  a default inactive `START → chat_agent → END` graph per company.

### How the method/tool shows in chat

Each assistant reply shows a **"Method:"** badge at the bottom of the bubble
(`chat/templates/chat/chat_page.html`): the graph tool used when routed through a
flow, else "Document reference" when citations exist, else "General purpose".

---

## Route reference

| Route | Entry | Purpose |
|-------|-------|---------|
| `/chat/`, `/chat/<id>/` | `chat/views.py` | the chat page (Unfold shell) |
| `/chat/<id>/send/` | `chat/views.py::send` | run one turn (graph or RAG) |
| `/agent-builder/` | `agents/views.py` | the flow builder canvas (iframe) |
| `/company/set-active-company/` | `company/views.py` | switch active company |
| `api/v1/agents/graph|node|edge|tool/` | `agents/api/views.py` | canvas CRUD + `graph/<id>/load/` + `graph/<id>/activate/` |
| `api/v1/chat/...`, `api/v1/documents/...` | each app's `api/` | thin DRF surface |
