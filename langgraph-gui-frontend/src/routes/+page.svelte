<!--
  routes/+page.svelte — FEAT-003.

  Hosts the vendored LangGraph-GUI graph editor as a single prerendered,
  ssr=false route (see +layout.ts) and wires it to the project's DRF API:

    - reads the iframe querystring contract ?csrf&company&company_name;
    - first-visit flow (design 6): GET /graph/ first, create 'Default agent'
      ONLY if none exist, then pick/activate (no singleton assumption);
    - persistence via lib/api.ts + lib/adapter.ts (snapshot-diff Save), NOT the
      discarded upstream FastAPI FileTransmit/graphs-io path;
    - STEP-node Tool checkboxes sourced from the load payload (node-texture.svelte);
    - Run via lib/run.ts surfaced through RunWindow.svelte.

  START/END uniqueness is enforced CLIENT-SIDE by the adapter guard before any
  node is added (the backend does NOT enforce it — design 3.4 / 5.2).
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { SvelteFlow, Controls, Background, MiniMap, type OnConnect } from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';

	import {
		currentNodes,
		currentEdges,
		serial_number
	} from '$lib/flow/graphs.store.svelte';
	import type { FlowNode } from '$lib/flow/node-schema';
	import { NodeType } from '$lib/flow/node-schema';
	import NodeLayout from '$lib/flow/node-texture.svelte';
	import FlowAlgo from '$lib/flow/flow-algo.svelte';
	import { AddEdge } from '$lib/flow/graph-algo.svelte';
	import GraphButton from '$lib/menu/graph-button.svelte';
	import RunWindow from '$lib/menu/RunWindow.svelte';
	import { openRunWindow } from '$lib/menu/menu.store';

	import { api } from '$lib/api';
	import {
		type AdapterNodeType,
		END_TYPE,
		guardAddOrChange,
		kindForType
	} from '$lib/adapter';
	import {
		activeCompanyName,
		graphId,
		graphName,
		graphIsActive,
		banner,
		loading,
		dirty,
		initFromQuery,
		bootstrap,
		save as saveCanvas,
		reload,
		markDirty
	} from '$lib/canvas.store.svelte';

	const handleConnect: OnConnect = (e) => {
		if (e.sourceHandle != null) {
			AddEdge(e.source, e.sourceHandle, e.target);
			markDirty();
		}
	};

	// custom node types
	const nodeTypes = {
		textNode: NodeLayout
	};

	let busy = $state(false);

	onMount(() => {
		initFromQuery();
		void bootstrap(api);
	});

	/** Add a node of a specific adapter type, honoring the START/END guard. */
	function addNode(type: AdapterNodeType): void {
		const nodes = get(currentNodes);
		const existingTypes = nodes.map((n) => n.data.type as AdapterNodeType);
		const guard = guardAddOrChange(existingTypes, type);
		if (guard) {
			banner.set(guard);
			return;
		}
		const serial = get(serial_number);
		const newNode: FlowNode = {
			id: String(serial),
			type: 'textNode',
			data: {
				name: 'Node' + String(serial),
				description: '',
				type: type as NodeType,
				nexts: new Set<string>(),
				true_next: null,
				false_next: null
			},
			position: { x: 120 + Math.random() * 320, y: 80 + Math.random() * 240 },
			// Default to the adapter's canonical size (200) so a fresh, un-resized
			// node looks like a normal card AND round-trips without introducing
			// ui_width/ui_height keys the load payload didn't carry (adapter.ts).
			width: 200,
			height: 200
		};
		serial_number.set(serial + 1);
		currentNodes.update((ns) => [...ns, newNode]);
		banner.set('');
		markDirty();
	}

	async function handleSave(): Promise<void> {
		if (!get(graphId)) return;
		busy = true;
		banner.set('');
		try {
			await saveCanvas(api);
			banner.set('Saved.');
		} catch (err) {
			banner.set(err instanceof Error ? err.message : `${err}`);
		} finally {
			busy = false;
		}
	}

	async function handleActivate(): Promise<void> {
		const id = get(graphId);
		if (!id) return;
		busy = true;
		banner.set('');
		try {
			// Flush edits first so activate validates the on-disk graph.
			if (get(dirty)) await saveCanvas(api);
			await api.activateGraph(id);
			await reload(api);
			banner.set('Graph activated.');
		} catch (err) {
			const body = (err as { body?: unknown })?.body;
			if (body && typeof body === 'object' && 'detail' in (body as Record<string, unknown>)) {
				banner.set(String((body as Record<string, unknown>).detail));
			} else {
				banner.set(err instanceof Error ? err.message : `${err}`);
			}
		} finally {
			busy = false;
		}
	}

	// Position drag persistence marks the canvas dirty (persisted on next Save).
	function onNodeDragStop(): void {
		markDirty();
	}
</script>

<div class="canvas-shell">
	<div class="canvas-area">
		<div class="toolbar">
			<button class="tb-btn tb-btn-start" disabled={busy} onclick={() => addNode(NodeType.START)}>+ Start</button>
			<button class="tb-btn tb-btn-step" disabled={busy} onclick={() => addNode(NodeType.STEP)}>+ Step</button>
			<button class="tb-btn tb-btn-end" disabled={busy} onclick={() => addNode(END_TYPE)}>+ End</button>
			<button class="tb-btn" disabled={busy || $loading} onclick={handleSave}>Save</button>
			<button
				class="tb-btn tb-btn-primary"
				disabled={busy || $loading || !$graphId || $graphIsActive}
				onclick={handleActivate}
			>
				{$graphIsActive ? 'Active' : 'Activate'}
			</button>
			<button class="tb-btn" disabled={busy || $loading || !$graphId} onclick={() => openRunWindow.set(true)}>
				Run
			</button>
		</div>

		<GraphButton>
			<SvelteFlow
				onconnect={handleConnect}
				onnodedragstop={onNodeDragStop}
				bind:nodes={$currentNodes}
				edges={$currentEdges}
				{nodeTypes}
				fitView
			>
				<Controls />
				<Background />
				<MiniMap />
				<FlowAlgo />
			</SvelteFlow>
		</GraphButton>
	</div>

	<aside class="info-bar">
		{#if $banner}
			<div class="banner" role="status">{$banner}</div>
		{/if}
		{#if $loading}
			<div class="muted">Loading…</div>
		{:else if $graphName}
			<div class="graph-name">{$graphName}</div>
			<div class="muted">{$graphIsActive ? 'active' : 'inactive'}</div>
		{/if}
		{#if $activeCompanyName}
			<div class="muted">Company: {$activeCompanyName}</div>
		{/if}
		{#if $dirty}
			<div class="muted">Unsaved changes</div>
		{/if}
	</aside>
</div>

<RunWindow />

<style>
	/*
	  Dark theme matching the React canvas palette (agents/static/agents/canvas/)
	  so this canvas visually belongs in the dark django-unfold admin shell.
	  --bg:#0b1020 --panel:#0f1629 --panel-2:#121a30 --node:#141d35
	  --node-border:#2a3657 --text:#e6ebf5 --muted:#8a97b5 --accent:#7c5cff
	  --accent-2:#4f7cff --start:#22c55e --end:#ef4444 --step:#4f7cff
	  Styling only — no behavior changes.
	*/
	.canvas-shell {
		position: absolute;
		inset: 0;
		display: flex;
		background: #0b1020;
		color: #e6ebf5;
	}
	.canvas-area {
		position: relative;
		flex: 1 1 auto;
		min-width: 0;
		height: 100%;
		background: #0b1020;
	}
	.info-bar {
		flex: 0 0 240px;
		border-left: 1px solid #2a3657;
		background: #0f1629;
		padding: 12px;
		overflow-y: auto;
		color: #e6ebf5;
	}
	.toolbar {
		position: absolute;
		z-index: 5;
		top: 12px;
		left: 12px;
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.tb-btn {
		font-size: 13px;
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid #2a3657;
		background: #121a30;
		color: #e6ebf5;
		cursor: pointer;
	}
	.tb-btn:hover {
		border-color: #7c5cff;
	}
	.tb-btn-start:not(:disabled) {
		border-color: #22c55e;
		color: #7ef0b0;
	}
	.tb-btn-step:not(:disabled) {
		border-color: #4f7cff;
		color: #9db8ff;
	}
	.tb-btn-end:not(:disabled) {
		border-color: #ef4444;
		color: #ff9b9b;
	}
	.tb-btn-primary {
		background: #7c5cff;
		color: #ffffff;
		border-color: #7c5cff;
	}
	.tb-btn-primary:hover {
		border-color: #4f7cff;
	}
	.tb-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.banner {
		padding: 8px 10px;
		border-radius: 8px;
		font-size: 13px;
		margin-bottom: 10px;
		background: #121a30;
		border: 1px solid #2a3657;
		color: #e6ebf5;
	}
	.graph-name {
		font-weight: 600;
		font-size: 14px;
		color: #e6ebf5;
	}
	.muted {
		font-size: 12px;
		color: #8a97b5;
		margin-top: 4px;
	}

	/*
	  @xyflow controls/minimap/background legibility on dark. These target the
	  library's global classes, so :global() is required from a scoped block.
	*/
	.canvas-area :global(.svelte-flow) {
		background: #0a0f1e;
	}
	.canvas-area :global(.svelte-flow__background) {
		background-color: #0a0f1e;
	}
	.canvas-area :global(.svelte-flow__controls) {
		box-shadow: 0 0 2px 1px rgba(0, 0, 0, 0.4);
	}
	.canvas-area :global(.svelte-flow__controls-button) {
		background: #121a30;
		border-bottom: 1px solid #2a3657;
		color: #e6ebf5;
		fill: #e6ebf5;
	}
	.canvas-area :global(.svelte-flow__controls-button:hover) {
		background: #1b2746;
	}
	.canvas-area :global(.svelte-flow__controls-button svg) {
		fill: #e6ebf5;
	}
	.canvas-area :global(.svelte-flow__minimap) {
		background: #0f1629;
	}
	.canvas-area :global(.svelte-flow__minimap-mask) {
		fill: rgba(11, 16, 32, 0.6);
	}
	.canvas-area :global(.svelte-flow__edge-path),
	.canvas-area :global(.svelte-flow__connection-path) {
		stroke: #4f7cff;
	}
	.canvas-area :global(.svelte-flow__attribution) {
		background: rgba(15, 22, 41, 0.7);
		color: #8a97b5;
	}
	.canvas-area :global(.svelte-flow__attribution a) {
		color: #8a97b5;
	}
</style>
