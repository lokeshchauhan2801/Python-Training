<!-- src/routes/graph/menu/ConfigWindow.svelte -->
<script lang="ts">
	import { get } from 'svelte/store';
	import { openConfigWindow, username, llmModel, apiKey } from './menu.store';

	// Initialize local copies from the stores
	let llmModelValue = $state(get(llmModel));
	let apiKeyValue = $state(get(apiKey));

	function handleSave() {
		// Update stores
		llmModel.set(llmModelValue);
		apiKey.set(apiKeyValue);
		// Persist for next load
		localStorage.setItem('llmModel', llmModelValue);
		localStorage.setItem('apiKey', apiKeyValue);
		// Close modal
		openConfigWindow.set(false);
	}

	function handleCancel() {
		openConfigWindow.set(false);
	}
</script>

{#if $openConfigWindow}
	<div class="modal-overlay fixed inset-0 z-50 flex items-center justify-center">
		<div class="modal-panel w-96 rounded-lg p-6 shadow-lg">
			<h2 class="mb-4 text-xl font-semibold">Settings</h2>

			<!-- Username (read-only) -->
			<div class="mb-3">
				<label for="username" class="modal-label mb-1 block text-sm">Username:</label>
				<input
					type="text"
					id="username"
					bind:value={$username}
					readonly
					class="modal-input modal-input-readonly w-full cursor-not-allowed rounded p-2"
				/>
			</div>

			<!-- LLM model -->
			<div class="mb-3">
				<label for="llmModel" class="modal-label mb-1 block text-sm">LLM model:</label>
				<input
					type="text"
					id="llmModel"
					bind:value={llmModelValue}
					class="modal-input w-full rounded p-2 focus:outline-none"
				/>
			</div>

			<!-- API Key -->
			<div class="mb-4">
				<label for="apiKey" class="modal-label mb-1 block text-sm">API key:</label>
				<input
					type="text"
					id="apiKey"
					bind:value={apiKeyValue}
					class="modal-input w-full rounded p-2 focus:outline-none"
				/>
			</div>

			<!-- Actions -->
			<div class="flex justify-end space-x-2">
				<button
					onclick={handleSave}
					class="rounded bg-blue-500 px-4 py-2 font-bold text-white hover:bg-blue-700"
				>
					Save
				</button>
				<button
					onclick={handleCancel}
					class="rounded bg-gray-500 px-4 py-2 font-bold text-white hover:bg-gray-700"
				>
					Cancel
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	/* Dark modal, legible on the #0b1020 shell. Styling only — logic unchanged. */
	.modal-overlay {
		background: rgba(5, 8, 18, 0.6);
	}
	.modal-panel {
		background: #0f1629;
		color: #e6ebf5;
		border: 1px solid #2a3657;
	}
	.modal-label {
		color: #cdd6ec;
	}
	.modal-input {
		background: #0b1020;
		border: 1px solid #2a3657;
		color: #e6ebf5;
	}
	.modal-input:focus {
		border-color: #7c5cff;
	}
	.modal-input-readonly {
		background: #121a30;
		color: #8a97b5;
	}
</style>
