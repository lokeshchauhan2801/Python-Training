# LangGraph React Frontend Architecture

## Overview

This React + TypeScript frontend provides a visual graph editor for building LangGraph agents. It uses React Flow for the canvas, Tailwind CSS for styling, and integrates with the Django DRF backend.

## Why @langchain/react is Installed but Not Used

The `@langchain/react` package (v0.1.1) is included as a dependency to signal future streaming integration, but the SDK hooks (`useStream`, `useLangGraph`) are **not currently used** in the implementation.

### Reason: Backend API Mismatch

The `@langchain/react` hooks expect the **LangGraph Platform streaming API** format:
- Server-Sent Events (SSE) for token-by-token streaming
- LangGraph Platform-specific endpoint structure (`/threads/{threadId}/runs`)
- Standardized message formats aligned with LangGraph Cloud

Our Django backend provides a **different API contract**:
- Form-urlencoded endpoints: `POST /chat/new/`, `POST /chat/{id}/send/`
- Complete JSON responses (non-streaming): `{assistant: {content, citations, chart_data}}`
- Designed for the existing RAG + chat architecture

### Current Implementation

`RunModal.tsx` uses direct `fetch()` calls via the `api.ts` client to consume the Django chat endpoints. This approach:
- ✅ Correctly implements the existing backend contract
- ✅ Provides full functionality (activate → create session → send message → display response)
- ✅ Handles error states (400, 502, 503) with user-friendly messages
- ✅ Displays citations and chart data when present
- ❌ Does not stream tokens incrementally (backend limitation)

### Future Enhancement Path

To use `@langchain/react` hooks, we would need **one of**:

1. **Backend Migration** (preferred if streaming is required):
   - Add SSE streaming endpoints at `/api/v1/langgraph/threads/{id}/runs/`
   - Emit LangGraph Platform-compatible event stream
   - Migrate `RunModal.tsx` to `useStream()` hook
   - Benefit: Token-by-token display, interrupt handling, tool call visibility

2. **Custom Transport Adapter**:
   - Build adapter wrapping form-urlencoded calls → SDK hook interface
   - High complexity, low value (no streaming benefit)
   - Not recommended unless SDK integration is a strict requirement

3. **Keep Current Approach** (recommended for now):
   - Direct fetch() matches backend reality
   - SDK dependency signals future intent
   - Users get full functionality without over-engineering

### Decision

The current implementation is **correct and appropriate** for the existing backend. The `@langchain/react` dependency remains installed to enable easy migration when/if the backend adds streaming support.

## Streaming Status

**Finding:** The implementation does not stream tokens incrementally.

**Root Cause:** The Django backend returns complete JSON responses. The `/chat/{id}/send/` endpoint processes the full LLM response before returning.

**Impact:** Users see a loading spinner while the agent runs, then the full response appears at once. This matches the behavior of the previous Svelte implementation.

**Future Work:** If incremental streaming is required, the backend would need to:
- Implement SSE or chunked transfer encoding
- Stream LLM tokens as they arrive from the provider
- Emit partial `content` updates during agent execution

Frontend changes would then be minimal: replace `await api.sendChatMessage()` with an SSE listener or integrate `useStream()` hook.

## Auto-Save Behavior

**Node position updates:** Auto-save on drag stop (`onNodeDragStop` → `persistPosition` → immediate PATCH `/api/v1/agents/node/{id}/`)

**Tool toggles:** Auto-save on checkbox change (immediate PATCH `/api/v1/agents/node/{id}/`)

**Node/edge creation:** Auto-save on add (immediate POST to `/api/v1/agents/node/` or `/api/v1/agents/edge/`)

**Node/edge deletion:** Auto-save on delete (immediate DELETE)

**Save button:** Removed in latest version. All changes persist immediately. The "Activate" button serves as the explicit "ready to run" signal.

## Node Types

The backend supports five node types (defined in `agents/models.py`):
- `start`: Entry point (green circle)
- `step`: Processing step with tool selection (blue rectangle)
- `end`: Terminal node (red circle)
- `tool`: Tool invocation node (blue rectangle, same styling as step)
- `condition`: Branching logic node (blue rectangle, same styling as step)

All five types are exposed via toolbar buttons. The canvas uses React Flow to render and connect them.

## API Client Pattern

`api.ts` provides a typed client with two request helpers:

1. **`request<T>(method, path, body?)`** — JSON API (DRF endpoints)
   - Used for: graphs, nodes, edges, tools CRUD
   - Content-Type: `application/json`
   - Response: JSON

2. **`formRequest<T>(url, body)`** — Form-urlencoded (chat endpoints)
   - Used for: `/chat/new/`, `/chat/{id}/send/`
   - Content-Type: `application/x-www-form-urlencoded`
   - Response: JSON

**CSRF Token:** Read from URL querystring (`?csrf=...`) and sent as `X-CSRFToken` header on non-GET requests. This matches the Django iframe pattern used by the existing agent-builder view.

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
   - Load `/agent-builder/` in Django (with CSRF token)
   - Add nodes (all five types via toolbar)
   - Connect edges (drag handles)
   - Select STEP node → toggle tools in sidebar
   - Click Activate → graph marked active
   - Click Run → modal opens → enter prompt → response displays

**Backend tests:** The Django test suite (`python manage.py test agents`) covers API endpoints. Frontend integration is verified manually.

## File Structure

```
agent-builder-frontend/
├── src/
│   ├── main.tsx          # Entry point, mounts <App />
│   ├── App.tsx           # Main canvas component (React Flow + toolbar + sidebar)
│   ├── RunModal.tsx      # Run dialog (activate → create session → send message)
│   ├── api.ts            # Typed API client (JSON + form-urlencoded)
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
- `agents/api/views.py`, `agents/api/serializers.py` — JSON CRUD for graphs, nodes, edges, tools

**Chat endpoints (unchanged):**
- `chat/views.py` — Form-urlencoded `/chat/new/`, `/chat/{id}/send/`

**URL routing (changed):**
- `base/urls.py` — Removed `/agent-builder-v2/` route (old Svelte app)
- `agents/urls.py` — Kept `/agent-builder/` route (extended React app)

**Settings (changed):**
- `base/settings.py` — Unfold sidebar now points to `/agent-builder/` (removed v2 menu item)

## Migration from Svelte

The Svelte app (`langgraph-gui-frontend/`) was fully removed. This React implementation replicates all Svelte features:

| Feature | Svelte | React |
|---------|--------|-------|
| Graph canvas | Svelte Canvas API | React Flow |
| Node types | START, STEP, END | START, STEP, END, TOOL, CONDITION |
| Toolbar | + Start, + Step, + End, Save, Activate, Run | + Start, + Step, + End, + Tool, + Condition, Activate, Run |
| Auto-save | On drag, on tool toggle | Same |
| Run modal | Form-urlencoded chat endpoints | Same |
| Streaming | No | No (backend limitation) |
| TypeScript | Yes | Yes |
| Dark theme | Custom CSS | Tailwind + custom CSS |
| Build output | Vite → `agents/static/agents/svelte-canvas/` | Vite → `agents/static/agents/canvas/` |
| Django route | `/agent-builder-v2/` | `/agent-builder/` (extended existing) |

The React implementation extends the existing `/agent-builder/` app (previously a simpler canvas) rather than creating a new route. The old Svelte route is deleted.

## Known Limitations

1. **No streaming token display** — Backend returns complete responses
2. **@langchain/react hooks not used** — Backend API mismatch (documented above)
3. **No undo/redo** — All changes persist immediately
4. **No batch edit mode** — Each change is an individual API call
5. **No graph versioning UI** — Snapshots exist in backend but not exposed in frontend

## Future Enhancements

- Migrate to LangGraph Platform streaming API + `useStream()` hook
- Add undo/redo with client-side state management (Redux, Zustand)
- Batch edit mode: accumulate changes → flush on Save
- Graph version history UI (list snapshots, diff viewer, restore)
- Node configuration panels (CONDITION: branch logic, TOOL: parameters)
- Multi-graph switcher (currently loads first graph only)
- Collaborative editing (WebSocket sync, conflict resolution)
