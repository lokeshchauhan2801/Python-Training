import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		runes: true
	},
	preprocess: vitePreprocess(),

	kit: {
		// Served by Django from a static file, so no Node server / SSR at runtime.
		// No `fallback`: the single canvas route is fully prerendered, which lets
		// kit.paths.relative emit RELATIVE asset refs (a SPA fallback page would
		// instead emit base-rooted absolute refs that 404 under Django's static URL).
		adapter: adapter(),
		// Asset refs must be relative: Django serves from a static URL, not site root.
		paths: {
			relative: true
		},
		prerender: {
			entries: ['/']
		}
	}
};

export default config;
