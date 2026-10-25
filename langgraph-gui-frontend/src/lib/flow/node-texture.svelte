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

	// Per-type accent so START/STEP/END/CONDITION/TOOL/INFO/SUBGRAPH stay
	// visually distinguishable on the dark theme. Styling only — the node's
	// type/behavior is unchanged.
	const TYPE_ACCENT: Record<string, string> = {
		[NodeType.START]: '#22c55e',
		[NodeType.STEP]: '#4f7cff',
		[NodeType.CONDITION]: '#f59e0b',
		[NodeType.TOOL]: '#7c5cff',
		[NodeType.INFO]: '#8a97b5',
		[NodeType.SUBGRAPH]: '#38bdf8'
	};
	// END is the adapter's end type; its label is 'END' but is not a NodeType enum
	// member, so key off the string value too.
	let accent = $derived(TYPE_ACCENT[data.type as string] ?? (data.type === 'END' ? '#ef4444' : '#2a3657'));

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
	class="node-card relative rounded-md p-2.5 text-center"
	class:selected
	style="width: {width}px; height: {height}px; --node-accent: {accent};"
>
	<NodeResizer minWidth={200} minHeight={160} isVisible={selected} color="rgb(124,92,255)" />

	<!-- type badge: per-type accent so START/STEP/END/etc stay distinguishable -->
	<div class="node-type-badge">{data.type}</div>

	<NodeHandles node_type={data.type} />

	<!-- NAME INPUT -->
	{#if data.type !== NodeType.START && data.type !== NodeType.TOOL}
		<div class="mt-2 flex items-center space-x-2">
			<label for="node-name-{id}" class="node-label text-left text-sm"> Name: </label>
			<input
				id="node-name-{id}"
				type="text"
				class="node-input w-full p-1 text-sm focus:outline-none"
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
		<label for="node-type-{id}" class="node-label text-sm"> Type: </label>
		<select
			id="node-type-{id}"
			class="node-input flex-1 p-1 text-sm focus:outline-none"
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
			<div class="node-label text-sm">Tools:</div>
			{#if $tools.length === 0}
				<div class="node-muted text-xs">No tools available.</div>
			{:else}
				<div class="max-h-24 overflow-y-auto">
					{#each $tools as t (t.id)}
						<label class="node-label flex items-center space-x-2 text-sm">
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
			<label for="node-description-{id}" class="node-label mb-1 block text-left text-sm">
				Description:
			</label>

			<!-- 3.1 Bind locally, update only on blur -->
			<textarea
				id="node-description-{id}"
				class="node-input h-full w-full flex-grow resize-none overflow-y-auto
               rounded p-1 text-sm
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

<style>
	/*
	  Dark node card matching the React canvas palette (node:#141d35,
	  border:#2a3657, text:#e6ebf5, muted:#8a97b5). The per-type accent
	  (--node-accent) colors the top border + type badge so START/STEP/END/
	  CONDITION/TOOL/INFO/SUBGRAPH stay distinguishable. Styling only.
	*/
	.node-card {
		background: #141d35;
		border: 1px solid #2a3657;
		border-top: 3px solid var(--node-accent, #2a3657);
		color: #e6ebf5;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
	}
	.node-card.selected {
		border-color: var(--node-accent, #7c5cff);
		box-shadow:
			0 0 0 2px rgba(124, 92, 255, 0.4),
			0 10px 30px rgba(0, 0, 0, 0.45);
	}
	.node-type-badge {
		position: absolute;
		top: -11px;
		left: 50%;
		transform: translateX(-50%);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		padding: 1px 8px;
		border-radius: 999px;
		background: #0f1629;
		border: 1px solid var(--node-accent, #2a3657);
		color: var(--node-accent, #e6ebf5);
		pointer-events: none;
		white-space: nowrap;
	}
	.node-label {
		color: #cdd6ec;
	}
	.node-muted {
		color: #8a97b5;
	}
	.node-input {
		background: #0f1629;
		border: 1px solid #2a3657;
		border-radius: 4px;
		color: #e6ebf5;
	}
	.node-input::placeholder {
		color: #5c6a8a;
	}
	.node-input:focus {
		border-color: #7c5cff;
	}
	.node-input option {
		background: #0f1629;
		color: #e6ebf5;
	}
</style>
