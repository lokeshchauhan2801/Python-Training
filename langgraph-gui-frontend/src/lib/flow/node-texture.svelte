<!-- routes/graph/flow/node-layout.svelte -->
<!--
  FEAT-003: the upstream free-text "Tool" input on STEP nodes is replaced by
  checkboxes over the company's Tool rows (the load payload's `tools`), stored
  as the Tool M2M (design 3.3 "STEP tool selection" / React App.jsx). The rest
  of the vendored node is kept verbatim.
-->
<script lang="ts">
	import type { NodeProps } from '@xyflow/svelte';
	import { useSvelteFlow, NodeResizer } from '@xyflow/svelte';
	import NodeHandles from './node-handles.svelte';
	import { NodeType } from './node-schema';
	import { tools, nodeTools, markDirty } from '../canvas.store.svelte';

	let { id, data, selected, width, height }: NodeProps = $props();
	const { updateNodeData } = useSvelteFlow();

	// 1. Reactive local copy of description
	let localDescription = $state(data.description);

	// STEP-node tool selection (M2M), keyed by this canvas node id.
	let selectedTools = $derived($nodeTools.get(id) ?? []);

	function toggleTool(toolId: string, checked: boolean): void {
		nodeTools.update((m) => {
			const next = new Map(m);
			const current = new Set((next.get(id) ?? []).map(String));
			if (checked) current.add(String(toolId));
			else current.delete(String(toolId));
			next.set(id, Array.from(current));
			return next;
		});
		markDirty();
	}
</script>

<div
	class="relative rounded-md border border-gray-300 bg-gray-200 p-2.5 text-center"
	style="width: {width}px; height: {height}px;"
>
	<NodeResizer minWidth={260} minHeight={280} isVisible={selected} color="rgb(255,64,0)" />

	<NodeHandles node_type={data.type} />

	<!-- NAME INPUT -->
	{#if data.type !== NodeType.START && data.type !== NodeType.TOOL}
		<div class="mt-2 flex items-center space-x-2">
			<label for="node-name-{id}" class="text-left text-sm text-gray-700"> Name: </label>
			<input
				id="node-name-{id}"
				type="text"
				class="w-full bg-white p-1 text-sm focus:outline-none"
				value={data.name}
				oninput={(e) => {
					const val = (e.currentTarget as HTMLInputElement).value;
					updateNodeData(id, { name: val });
					markDirty();
				}}
			/>
		</div>
	{/if}

	<!-- TYPE DROPDOWN -->
	<div class="mt-2 flex items-center space-x-2">
		<label for="node-type-{id}" class="text-sm text-gray-700"> Type: </label>
		<select
			id="node-type-{id}"
			class="flex-1 bg-white p-1 text-sm focus:outline-none"
			value={data.type}
			onchange={(e) => {
				const newType = (e.currentTarget as HTMLSelectElement).value as NodeType;
				updateNodeData(id, { type: newType });
				markDirty();
			}}
		>
			{#each Object.values(NodeType) as type (type)}
				<option value={type} selected={type === data.type}>
					{type}
				</option>
			{/each}
		</select>
	</div>

	<!-- TOOL CHECKBOXES (STEP nodes) — company Tool rows, stored as the M2M -->
	{#if data.type === NodeType.STEP}
		<div class="mt-2 text-left">
			<div class="text-sm text-gray-700">Tools:</div>
			{#if $tools.length === 0}
				<div class="text-xs text-gray-500">No tools available.</div>
			{:else}
				<div class="max-h-24 overflow-y-auto">
					{#each $tools as t (t.id)}
						<label class="flex items-center space-x-2 text-sm">
							<input
								type="checkbox"
								checked={selectedTools.map(String).includes(String(t.id))}
								onchange={(e) =>
									toggleTool(String(t.id), (e.currentTarget as HTMLInputElement).checked)}
							/>
							<span>{t.name || t.slug || t.id}{t.is_enabled === false ? ' (disabled)' : ''}</span>
						</label>
					{/each}
				</div>
			{/if}
		</div>
	{/if}

	<!-- DESCRIPTION -->
	{#if data.type !== NodeType.START && data.type !== NodeType.SUBGRAPH}
		<div class="mt-2 flex h-[calc(100%-120px)] min-h-0 flex-grow flex-col">
			<label for="node-description-{id}" class="mb-1 block text-left text-sm text-gray-700">
				Description:
			</label>

			<!-- 3.1 Bind locally, update only on blur -->
			<textarea
				id="node-description-{id}"
				class="h-full w-full flex-grow resize-none overflow-y-auto
               rounded border border-gray-300 bg-white p-1 text-sm
               focus:outline-none"
				bind:value={localDescription}
				onblur={() => {
					updateNodeData(id, { description: localDescription });
					markDirty();
				}}
			></textarea>
		</div>
	{/if}
</div>
