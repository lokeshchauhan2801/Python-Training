# LangGraph React Frontend Architecture

## Overview

This React + TypeScript frontend provides a visual editor for building a company's chat flow. It uses React Flow for the canvas, Tailwind CSS for styling, and integrates with the Django DRF backend.

The backend model is a two-level `Agent` container + `Step` nodes, plus a flat `Tool` table (see `agents/models.py`). An `Agent` is a named flow; its ordered `Step` rows are the nodes and each step holds the tools. The canvas renders one React Flow node per `Step` row plus two synthetic START/END anchors, and translates connections/drags into `Step` PATCHes. The dropdown lists `Agent`s; selecting one shows only that agent's steps.

## @langchain/react is Installed but Not Used

The `@langchain/react` package (v0.1.1) is still listed as a dependency but is **not used** by this canvas. There is no Run path in this frontend anymore (the Run modal was removed); running a flow happens from the chat page via the normal chat endpoints. The dependency is inert here and can be dropped if desired.

## Auto-Save Behavior

All changes persist immediately via `Step`/`Agent` PATCHes; there is no explicit Save button.

**Step position:** Auto-save on drag stop (`onNodeDragStop` → `persistPosition` → PATCH `/api/v1/agents/step/{id}/` with `position_x`/`position_y`). The flush also updates local `steps` state so a dragged node never visually resets. START/END anchors are not draggable and never persisted.

**Tool membership:** Auto-save on checkbox change (PATCH `/api/v1/agents/step/{id}/` with the full `tools` array).

**Chain links (connect/disconnect), scoped to the selected agent:**
- `START → step`: PATCH step `connected_from_start: true` (delete → `false`)
- `step → step`: PATCH source step `next_step: <target id>` (delete → `null`)
- `step → END`: PATCH step `connected_to_end: true, next_step: null` (delete → `false`)

After every link/tool/flag mutation the canvas re-calls `GET /agent/load/?agent=<id>` so it mirrors backend truth.

**Agent create/delete:** POST `/api/v1/agents/agent/` (stamping `company` from the querystring) / DELETE `/api/v1/agents/agent/{id}/`. **Step create/delete:** POST `/api/v1/agents/step/` (requires `agent`, stamps `company`) / DELETE `/api/v1/agents/step/{id}/`.

**Activate:** POST `/api/v1/agents/agent/{id}/activate/`. `is_active` is READ-ONLY on PATCH and is only ever set through this action.

## The Flow Model (Agent container + Step nodes)

The flow is an **Agent container** whose ordered **Step nodes** form a linear chain. There are no `start`/`step`/`end`/`tool`/`condition` node types and no graph/node/edge objects.

- The entry step has `connected_from_start = true`.
- Each step points to the next via `next_step` (same agent only).
- The final step has `connected_to_end = true`.
- Each step carries its own tools (M2M to the flat `Tool` table) — the step holds the tools.
- Exactly one `Agent` per company is active (`is_active` on the container).

The canvas renders:
- One React Flow node per `Step` row (styled `.ab-node ab-node-step`).
- Two **synthetic** anchor nodes `__start__` / `__end__` — client-only visual anchors derived from `connected_from_start` / `connected_to_end`; they are never backend rows and never sent in writes.
- Derived edges: `START → entry step`, `step → step.next_step`, `final step → END`.

The toolbar has three buttons — `Add Agent` (create a new flow container), `+ Step` (add a step to the selected agent), and `Activate` — plus an agent dropdown to switch which flow the canvas views. The `+ Tool`, `+ Condition`, and `Run` buttons and the Run modal were intentionally removed and must not be reintroduced.

## Tool Types

`Tool.type` is one of: `general_assistant`, `document_search`, `chat_summarise`, `web_search`, `add_to_knowledge_base`, `document_processing_status`, `langfuse`. For non-`langfuse` tools the `slug` must be a registered handler slug; `langfuse` tools are prompt-only. The sidebar shows each tool's `type` next to its name so langfuse tools are distinguishable.

## API Client Pattern

`api.ts` provides a typed client with one JSON request helper:

- **`request<T>(method, path, body?)`** — JSON API against `/api/v1/agents`.
  - `/tool/` and `/tool/{id}/` — Tool CRUD
  - `/agent/` and `/agent/{id}/` — Agent (container) CRUD
  - `/step/` and `/step/{id}/` — Step (node) CRUD
  - `/agent/{id}/activate/` — mark that agent active for the company
  - `/agent/load/?company=<id>&agent=<id>` — bootstrap payload `{ active_agent_id, agents, selected_agent_id, steps, tools }`
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
   - Click `Add Agent` to create a flow container, then `+ Step` to add steps
   - Drag from START to a step, step → step, and step → END to wire the chain
   - Select a step node → rename it and toggle tools / entry/final flags in the sidebar
   - Click Activate → the selected agent is marked active for the company

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
- `agents/api/views.py`, `agents/api/serializers.py` — JSON CRUD for tools and agents, plus `/agent/{id}/activate/` and `/agent/load/`

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
