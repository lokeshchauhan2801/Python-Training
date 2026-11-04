# LangGraph React Frontend Architecture

## Overview

This React + TypeScript frontend provides a visual editor for building a company's chat flow. It uses React Flow for the canvas, Tailwind CSS for styling, and integrates with the Django DRF backend.

The backend model is a two-level `Agent` + `Step` model, plus a flat `Tool` table (see `agents/models.py`). An `Agent` is a **supervisor**; its `Step` rows are its **workers**, and each worker holds its tools or hands its work to another agent. The canvas draws START → Supervisor → one node per worker → END. The dropdown lists `Agent`s; selecting one shows that agent's supervisor and workers.

## @langchain/react is Installed but Not Used

The `@langchain/react` package (v0.1.1) is still listed as a dependency but is **not used** by this canvas. There is no Run path in this frontend anymore (the Run modal was removed); running a flow happens from the chat page via the normal chat endpoints. The dependency is inert here and can be dropped if desired.

## Auto-Save Behavior

All changes persist immediately via `Step`/`Agent` PATCHes; there is no explicit Save button.

**Worker position:** Auto-save on drag stop (`onNodeDragStop` → `persistPosition` → PATCH `/api/v1/agents/step/{id}/` with `position_x`/`position_y`); a plain click (no movement) saves nothing. START, Supervisor and END are canvas-only nodes: where you drag them is kept per agent in this browser's `localStorage`. "Tidy layout" puts the workers in one column.

**Tool membership:** Auto-save on checkbox change (PATCH `/api/v1/agents/step/{id}/` with the full `tools` array). Ticking a tool that has no `Tool` row yet creates it first (`POST /tool/attach/`).

**Wiring:** there is none to edit. A worker is connected to its supervisor just by belonging to the agent, so the canvas does not allow drawing or deleting wires. (`connected_from_start` / `next_step` / `connected_to_end` are legacy columns the runtime no longer reads.)

After every change the canvas re-calls `GET /agent/load/?agent=<id>` so it mirrors backend truth.

**Agent create/rename/describe/delete:** POST `/api/v1/agents/agent/` (stamping `company` from the querystring) / PATCH `name` or `description` / DELETE `/api/v1/agents/agent/{id}/`. **Worker create/delete:** POST `/api/v1/agents/step/` (requires `agent`, stamps `company`) / DELETE `/api/v1/agents/step/{id}/`.

**Agent selection:** Agents are independent flow containers — there is no company-wide "current" Agent and no state endpoint to flip one on. Choosing an Agent in the dropdown only changes which flow the canvas edits (it re-calls `GET /agent/load/?agent=<id>`); it never changes how Chat behaves. Chat sessions pick an Agent per session through the Chat UI's own selector.

## The Agent Model (supervisor + workers)

- An **Agent** has a name and a **description** ("what this agent does"). The supervisor reads the descriptions to route work, and a worker without tools answers with its agent's description as its instructions.
- Its **Steps are workers**. A worker either uses its own tools (M2M to the flat `Tool` table), hands its parts to another Agent (`delegate_agent`), or, with no tools, answers itself with the LLM in its role.
- A hand-off target must be another agent of the same company, and hand-offs can't loop back (checked server-side; at most 3 agents deep at run time). That agent supervises its own workers for the part it gets.
- A company can hold many agents side by side; chat sessions pick one each in the chat header.

### How a chat message runs

`chat/services/task_planner.py` is the supervisor. For each message it:

1. splits the message into parts and gives each part to one worker (by its tools and description), naming for each part the earlier parts whose answer it needs (`depends_on`);
2. runs the parts as a LangGraph graph built from those dependencies: parts that need nothing from each other start at the same time; a part that needs another waits for it and receives only that answer;
3. returns one reply with every part's answer, numbered in the order asked and naming who answered it. The turn's `metadata.tasks` records each part's worker, agent, tool, dependencies and wave; the chat page shows it as "How this was answered".

An agent with a single worker skips the planner for a plain single request. `CHAT_MULTI_TASK_ENABLED` (default on) and `CHAT_MULTI_TASK_MAX` (default 5 parts) control it.

The canvas renders:
- START, a **Supervisor** node and END (canvas-only, never backend rows);
- one node per worker (`.ab-node ab-node-step`; blue for a hand-off);
- implied wires: START → Supervisor, Supervisor → every worker, every worker → END (its answer goes into the reply).

The toolbar has `New agent`, `+ Worker`, `Tidy layout` and the agent dropdown. The `+ Tool`, `+ Condition`, and `Run` buttons and the Run modal were intentionally removed and must not be reintroduced.

## Tool Types

`Tool.type` is one of: `general_assistant`, `document_search`, `chat_summarise`, `web_search`, `add_to_knowledge_base`, `document_processing_status`, `report_writer`, `action_items`, `email_drafter`, `translator`, `chart_builder`, `langfuse`. The last five built-ins work on the answers of the parts a part depends on. For non-`langfuse` tools the `slug` must be a registered handler slug; `langfuse` tools are prompt-only. The sidebar shows each tool's `type` next to its name so langfuse tools are distinguishable.

## API Client Pattern

`api.ts` provides a typed client with one JSON request helper:

- **`request<T>(method, path, body?)`** — JSON API against `/api/v1/agents`.
  - `/tool/` and `/tool/{id}/` — Tool CRUD
  - `/agent/` and `/agent/{id}/` — Agent (container) CRUD
  - `/step/` and `/step/{id}/` — Step (node) CRUD
  - `/agent/load/?company=<id>&agent=<id>` — bootstrap payload `{ agents, selected_agent_id, steps, tools }` (`selected_agent_id` is the in-scope `?agent=`, else the first Agent by `order, created_at`, else `null`; `steps` are the selected Agent's Steps only)
  - List endpoints handle both `{results:[]}` and `[]`.

The old graph/node/edge endpoints and the form-urlencoded `formRequest` chat helper were removed.

**CSRF Token:** Read from the URL querystring (`?csrf=...`) and sent as the `X-CSRFToken` header on non-GET requests. `company` and `company_name` are also read from the querystring (exposed as `companyId()` / `companyName()`); `company` is stamped on create calls so a multi-company user does not get a 400 "ambiguous". This matches the Django iframe pattern used by the agent-builder view.

## Build Output

**Build command:** `npm run build` (Vite + `vite-plugin-singlefile`)

**Output:** Single HTML file at `agents/static/agents/canvas/index.html`

**Served by:** Django view at `/agent-builder/` (agents/views.py) via iframe with CSRF token in querystring

**Why single-file:** Simplifies Django static file handling. The entire React app (JS + CSS + assets) is inlined into one HTML file that Django serves as a template include.

## Dependencies

**Runtime:**
- `react` ^18.3.1, `react-dom` ^18.3.1
- `reactflow` ^11.11.4 — Visual graph editor
- `@langchain/react` ^0.1.1 — Future streaming integration
- `@langchain/core` ^1.1.27 — Shared types

**Dev:**
- `typescript` ^5.7.2
- `tailwindcss` ^3.4.16 — Dark theme utilities
- `vite` ^5.4.11, `@vitejs/plugin-react` ^4.3.4
- `vite-plugin-singlefile` ^2.0.3 — Single HTML output

## Testing Strategy

**Verification steps** (from `/home/auriga/learnings/agentic-flow/.agents/tasks/langgraph-react-migration/verification.md`):

1. `npm install` — Dependencies resolve
2. `npm run build` — Compiles without errors, outputs to `agents/static/agents/canvas/index.html`
3. `npm run dev` — Dev server starts, canvas loads at `http://localhost:5173`
4. Manual E2E test:
   - Load `/agent-builder/` in Django (with `?csrf=...&company=...&company_name=...`)
   - Click `New agent` (name + description), then `+ Worker` to add workers; each one appears wired from the Supervisor
   - Select a worker → rename it, tick tools, or hand it off to another agent ("Another agent")
   - Edit the agent's name and description in the sidebar header
   - Switch Agents in the dropdown → the canvas shows that Agent's supervisor and workers (no Agent is "made current" by this)

**Backend tests:** The Django test suite (`python manage.py test agents`) covers API endpoints. Frontend integration is verified manually.

## File Structure

```
agent-builder-frontend/
├── src/
│   ├── main.tsx          # Entry point, mounts <App />
│   ├── App.tsx           # Main canvas component (React Flow + toolbar + sidebar)
│   ├── api.ts            # Typed API client (Tool + Agent JSON endpoints)
│   └── styles.css        # Tailwind directives + custom .ab-* classes
├── index.html            # Vite entry (references main.tsx)
├── package.json          # Dependencies + scripts
├── tsconfig.json         # TypeScript config (React JSX, ESNext)
├── tailwind.config.js    # Tailwind dark mode + content paths
├── postcss.config.js     # PostCSS + Tailwind + Autoprefixer
├── vite.config.js        # Vite + React plugin + singlefile output
└── ARCHITECTURE.md       # This file
```

## Backend Integration

**Django views (unchanged):**
- `agents/views.py` — Serves `/agent-builder/` (existing React canvas)
- `agents/templates/agents/agent_builder.html` — Template with iframe + CSRF token

**DRF API (unchanged):**
- `agents/api/views.py`, `agents/api/serializers.py` — JSON CRUD for tools and agents, plus `/agent/load/`

**Chat endpoints:** not used by this canvas (the Run path was removed; running a flow happens from the chat page).

**URL routing (changed):**
- `base/urls.py` — Removed `/agent-builder-v2/` route (old Svelte app)
- `agents/urls.py` — Kept `/agent-builder/` route (extended React app)

**Settings (changed):**
- `base/settings.py` — Unfold sidebar now points to `/agent-builder/` (removed v2 menu item)

## Migration from Svelte

An earlier Svelte prototype predated this canvas. The current React implementation targets the two-level Agent container + Step node model:

| Feature | Current React canvas |
|---------|----------------------|
| Canvas | React Flow (reactflow v11) |
| Model | Two-level `Agent` container + `Step` nodes + flat `Tool` table (no graph/node/edge objects) |
| Node representation | One React Flow node per `Step` row + synthetic START/END anchors |
| Toolbar | `Add Agent`, `+ Step`, `Activate`, agent dropdown (no `+ Tool`/`+ Condition`/`Run`) |
| Auto-save | Position PATCH on drag; `next_step`/flag PATCH on connect/disconnect; tools PATCH on toggle |
| Run | Removed from this canvas (run from the chat page) |
| TypeScript | Yes |
| Dark theme | Tailwind + custom `.ab-*` CSS |
| Build output | Vite → `agents/static/agents/canvas/` |
| Django route | `/agent-builder/` |

## Known Limitations

1. **No streaming token display** — there is no run path in this canvas
2. **@langchain/react dependency unused** — no run/streaming here
3. **No undo/redo** — all changes persist immediately
4. **No batch edit mode** — each change is an individual API call
5. **Single flow per view** — the canvas shows one agent's steps at a time (switchable via the agent dropdown)

## Future Enhancements

- API routing per agent (the model reserves room for multi-agent routing later)
- Undo/redo with client-side state management
- Batch edit mode: accumulate changes → flush once
- Richer per-agent configuration panels
