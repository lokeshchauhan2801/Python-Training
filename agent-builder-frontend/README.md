# agent-builder-frontend

The React Flow canvas for the LangGraph agent builder. It is embedded as a
same-origin `<iframe>` on the Django `/agent-builder/` page (rendered inside the
Unfold admin shell) and talks to the DRF `api/v1/agents/` endpoints with the
session cookie + the CSRF token the Django page passes via the iframe `src`
querystring (`?csrf=<token>`).

## Stack

- [Vite](https://vitejs.dev/) + React 18 + [`reactflow`](https://reactflow.dev/) v11.
- [`vite-plugin-singlefile`](https://www.npmjs.com/package/vite-plugin-singlefile)
  inlines the JS + CSS into a single self-contained `index.html`.

## Build

```bash
cd agent-builder-frontend
npm install
npm run build
```

The build writes a single file to `../agents/static/agents/canvas/index.html`
(see `vite.config.js` → `build.outDir`). That file is committed and collected by
Django's `collectstatic`; the Django page loads it via
`{% static 'agents/canvas/index.html' %}`.

Because everything is inlined into one HTML file, there are no hashed asset
paths to rewrite and no extra files to serve — the canvas works wherever
`collectstatic` places it.

## Dev

```bash
npm run dev
```

Note: the dev server is cross-origin to Django and cannot reach the
session-authenticated API or receive the CSRF token. For an end-to-end check,
run `npm run build` and open `/agent-builder/` in the running Django app.

## What the canvas does

- `GET /api/v1/agents/graph/` — picks the first owned graph (creates a
  `Default agent` graph if none exists).
- `GET /api/v1/agents/graph/{id}/load/` — hydrates nodes, edges, and tools.
- Node CRUD: `POST/PATCH/DELETE /api/v1/agents/node/` (add start/step/end,
  rename, drag-to-reposition persisted via `position`, attach/detach tools).
- Edge CRUD: `POST/DELETE /api/v1/agents/edge/` (connect/disconnect nodes).
- Tool toggles: `PATCH /api/v1/agents/tool/{id}/` (`is_enabled`).
- `POST /api/v1/agents/graph/{id}/activate/` — make this the company's single
  active graph (the chat path then routes new messages through it).
