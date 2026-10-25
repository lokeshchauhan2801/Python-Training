<!--
  routes/+page.svelte — FEAT-001 placeholder.

  Mounts the vendored LangGraph-GUI graph editor (flow layer) so the package
  builds as a single static canvas page. Full wiring of the menu components,
  the DRF API client, the adapter, and the run path happens in FEAT-003.
-->
<script lang="ts">
	import { SvelteFlow, Controls, Background, MiniMap, type OnConnect } from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';

	import { currentNodes, currentEdges } from '$lib/flow/graphs.store.svelte';
	import NodeLayout from '$lib/flow/node-texture.svelte';
	import FlowAlgo from '$lib/flow/flow-algo.svelte';
	import { AddEdge } from '$lib/flow/graph-algo.svelte';

	const handleConnect: OnConnect = (e) => {
		if (e.sourceHandle != null) {
			AddEdge(e.source, e.sourceHandle, e.target);
		}
	};

	// custom node types
	const nodeTypes = {
		textNode: NodeLayout
	};
</script>

<div class="content-wrapper">
	<SvelteFlow
		onconnect={handleConnect}
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
</div>

<style>
	.content-wrapper {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 100vh;
	}
</style>
