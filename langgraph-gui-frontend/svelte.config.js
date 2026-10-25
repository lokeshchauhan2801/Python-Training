import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		runes: true
	},
	preprocess: vitePreprocess(),

	kit: {
		adapter: adapter({
			fallback: 'index.html'
		}),
		// Asset refs must be relative: Django serves from a static URL, not site root.
		paths: {
			relative: true
		},
		router: {
			type: 'hash'
		}
	}
};

export default config;
