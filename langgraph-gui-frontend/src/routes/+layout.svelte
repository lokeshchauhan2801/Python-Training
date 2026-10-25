<!-- routes/+layout.svelte -->
<script>
	import '../app.css'; // Import global styles

	let { children } = $props();
</script>

<div class="flex min-h-screen flex-col">
	<main class="flex-grow">
		{@render children()}
	</main>
</div>
