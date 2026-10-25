# Attribution

This package vendors the frontend of the open-source **LangGraph-GUI** project
(the SvelteFlow canvas) and wires it to this project's existing Django DRF API.
The upstream FastAPI/Ollama backend is NOT vendored.

## Source

- Upstream repository: `LangGraph-GUI/LangGraph-GUI-Svelte`
  (https://github.com/LangGraph-GUI/LangGraph-GUI-Svelte)
- Vendored at pinned commit SHA: **`730c350fa5a7675b6578b64940f365a7a9d756d8`**
- License: MIT © 2025 HomunMage (see `LICENSE`, carried verbatim from upstream).

All upstream copyright/file-path header comments in the vendored component files
are preserved unchanged.

## Files copied verbatim from upstream

From upstream `src/routes/graph/flow/` into `src/lib/flow/`:

- `node-schema.ts`
- `node-handles.svelte`
- `node-texture.svelte`
- `graph-algo.svelte`
- `graphs.store.svelte.ts`
- `flow-algo.svelte`
- `flow-position.store.ts`

From upstream `src/routes/graph/menu/` into `src/lib/menu/`:

- `sidebar.svelte`
- `ConfigWindow.svelte`
- `RunWindow.svelte`
- `graphs-panel.svelte`
- `graph-button.svelte`
- `menu.store.ts`

### Deliberately NOT vendored

- `FileTransmit.svelte` and `graphs-io.svelte` — they target the discarded
  upstream FastAPI backend (file upload/download and local JSON import/export).
  Their persistence role is replaced by the newly authored DRF client/adapter
  (added in a later feature).

## Files newly authored for this project (not from upstream)

- `src/lib/api.ts` — DRF client to `/api/v1/agents/` (added in FEAT-002).
- `src/lib/adapter.ts` — backend ↔ SvelteFlow model mapping + save diff
  (added in FEAT-002).
- `src/lib/run.ts` — run-from-canvas via the existing chat endpoints
  (added in FEAT-003).
- `src/routes/+page.svelte`, `src/routes/+layout.svelte`, `src/routes/+layout.ts`,
  `src/app.html`, `src/app.css`, `src/app.d.ts` — SvelteKit shell / mount point.
- `svelte.config.js`, `vite.config.ts`, `tsconfig.json`, `package.json`,
  `package-lock.json`, `.gitignore` — build configuration for the
  `adapter-static` + single-file static build served by Django.

## Node-schema diff (MANDATORY first vendor step, design §3.1 / §3.3)

The upstream `src/routes/graph/flow/node-schema.ts` at the pinned SHA was diffed
against the design §3.3 mapping table.

**Result: NO DELTA.** The upstream shape matches the design table exactly:

- `JsonNodeData` fields: `uniq_id`, `name`, `description`, `nexts: string[]`,
  `type`, `tool`, `true_next: string | null`, `false_next: string | null`,
  `ext: { pos_x?, pos_y?, width?, height? }` — identical to design §3.1.
- `FlowNodeData` fields: `{ name, description, type, nexts: Set<string>,
  true_next, false_next }` (plus xyflow's own `id`, `position`, `width`,
  `height`) — identical to design §3.1.
- `NodeType` enum has exactly the six predicted values:
  `START | STEP | TOOL | CONDITION | INFO | SUBGRAPH`.

No renamed field, no extra field, and no change to the `NodeType` set were
found. The design §3.3 mapping table therefore stands unchanged, and the
FEAT-002 adapter can be written against these confirmed field names.

## Build-shape finding (design §1.1)

`npm install && npm run build` completes with exit 0. The emitted `build/`
directory is the **fallback / wider-tree shape**, NOT a single `index.html`:

```
build/index.html
build/_app/version.json
build/_app/immutable/{assets,chunks,entry,nodes}/…   (CSS + JS chunk set)
```

Under SvelteKit, `@sveltejs/adapter-static` owns the final output and overrides
Vite's `build.outDir` / `build.cssCodeSplit`; `vite-plugin-singlefile` cannot
inline the adapter's multi-chunk client build into one file. Therefore, per
design §1.1, the WHOLE `build/` subtree is committed under
`agents/static/agents/svelte-canvas/` (this drives FEAT-003's commit shape).

To make the tree servable from Django's static URL (a sub-path, not site root),
the static adapter is used WITHOUT a `fallback` page and the single canvas route
`/` is fully prerendered. Combined with `kit.paths.relative = true`, the emitted
`index.html` references assets RELATIVELY (`./_app/immutable/…`) and computes its
SvelteKit `base` from `location`, so it resolves correctly at
`…/static/agents/svelte-canvas/index.html`. (A `fallback: 'index.html'` SPA page
was tried first but emitted base-rooted ABSOLUTE refs `/_app/…` that would 404
under the static URL, so it was replaced by full prerender of `/`.)
