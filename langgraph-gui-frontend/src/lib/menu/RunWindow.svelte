<!-- src/lib/menu/RunWindow.svelte — vendored shell, REWIRED to lib/run.ts (FEAT-003). -->
<!--
  The single text area is the run PROMPT (design 4.2: the langgraph runtime is
  message-driven, so a run requires an input message). "Run" calls lib/run.ts's
  activate -> new session -> send sequence (design 4.3); the upstream FastAPI
  /upload + /run calls are gone. Output is rendered per design 4.3 step 5:
  assistant.content as text, chart_data in a <pre>, citations as plain text —
  NO chart rendering in v1 (the charted view lives at /chat/). CONDITION branches
  are editorial in v1 (the runtime ignores edge.condition).
-->
<script lang="ts">
	import { get } from 'svelte/store';
	import { openRunWindow } from './menu.store';
	import { api } from '../api';
	import { newSession, send } from '../api';
	import { run, type RunOutcome } from '../run';
	import {
		graphId,
		loadedGraphCompany,
		activeCompanyId,
		saveIfDirty,
		reload
	} from '../canvas.store.svelte';

	// local UI state
	let isRunning = $state(false);
	let prompt = $state('');
	let outcome = $state<RunOutcome | null>(null);

	async function handleRun() {
		const id = get(graphId);
		if (!id) {
			outcome = { ok: false, error: 'No graph loaded.' };
			return;
		}
		isRunning = true;
		outcome = null;
		try {
			outcome = await run(
				{
					api,
					newSession,
					send,
					saveIfDirty: () => saveIfDirty(api),
					reload: () => reload(api)
				},
				{
					graphId: id,
					loadedGraphCompany: get(loadedGraphCompany),
					activeCompanyId: get(activeCompanyId)
				},
				prompt
			);
		} catch (err) {
			outcome = { ok: false, error: err instanceof Error ? err.message : `${err}` };
		} finally {
			isRunning = false;
		}
	}

	function handleLeave() {
		openRunWindow.set(false);
	}
</script>

{#if $openRunWindow}
	<div class="bg-opacity-50 fixed inset-0 z-11 flex items-center justify-center">
		<div class="flex h-4/5 w-4/5 flex-col rounded bg-white p-5 shadow-md">
			<h2 class="mb-1 text-lg font-bold">Run graph</h2>
			<p class="mb-3 text-xs text-gray-500">
				CONDITION branches are editorial in v1 — the runtime does not branch on them.
			</p>

			<label for="run-prompt" class="mb-1 block text-sm text-gray-700">Prompt</label>
			<textarea
				id="run-prompt"
				class="mb-3 h-24 w-full resize-none rounded border border-gray-300 p-2 text-sm text-black focus:outline-none"
				placeholder="Message to run the graph against…"
				bind:value={prompt}
				disabled={isRunning}
			></textarea>

			<div class="mb-4 flex justify-end">
				<button
					onclick={handleRun}
					disabled={isRunning}
					class={`mr-2 rounded px-4 py-2 font-bold text-white ${
						isRunning ? 'cursor-not-allowed bg-gray-400' : 'bg-blue-500 hover:bg-blue-700'
					}`}
				>
					Run
				</button>
				<button
					onclick={handleLeave}
					disabled={isRunning}
					class={`rounded px-4 py-2 font-bold text-white ${
						isRunning ? 'cursor-not-allowed bg-gray-400' : 'bg-orange-500 hover:bg-orange-700'
					}`}
				>
					Leave
				</button>
			</div>

			<div class="mt-2 flex-1 overflow-y-auto rounded bg-gray-100 p-2 text-black">
				{#if isRunning}
					<p class="text-sm text-gray-500">Running…</p>
				{:else if outcome && !outcome.ok}
					<p class="text-sm text-red-700">{outcome.error}</p>
				{:else if outcome && outcome.ok}
					<pre class="whitespace-pre-wrap text-sm">{outcome.text}</pre>
					{#if outcome.chartJson}
						<div class="mt-3">
							<div class="text-xs font-semibold text-gray-500">chart_data</div>
							<pre class="whitespace-pre-wrap text-xs">{outcome.chartJson}</pre>
						</div>
					{/if}
					{#if outcome.citations && outcome.citations.length > 0}
						<div class="mt-3">
							<div class="text-xs font-semibold text-gray-500">Citations</div>
							{#each outcome.citations as c (c)}
								<p class="text-xs">{c}</p>
							{/each}
						</div>
					{/if}
				{/if}
			</div>
		</div>
	</div>
{/if}
