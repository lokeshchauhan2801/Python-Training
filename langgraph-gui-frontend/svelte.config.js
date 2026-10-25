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
		// Under hash routing there is no prerendered page, so the adapter emits a
		// `fallback` SPA shell as index.html. kit.paths.relative keeps its asset
		// refs relative (./_app/...) so they resolve under Django's static URL.
		adapter: adapter({
			fallback: 'index.html'
		}),
		// Asset refs must be relative: Django serves from a static URL, not site root.
		paths: {
			relative: true
		},
		// Hash-based routing: the app is served from an unpredictable deep static
		// path (/static/agents/svelte-canvas/index.html) inside a Django iframe.
		// With pathname routing, SvelteKit's client router compares that deep path
		// against its known routes, matches nothing, and renders its built-in 404.
		// Hash routing makes the router read the URL fragment (#/) instead of the
		// pathname, so the single canvas route resolves regardless of the static
		// sub-path it is served from. Hash routing is client-only, so there is no
		// prerender pass (and page-level prerender/ssr options are disallowed —
		// see src/routes/+layout.ts); adapter-static emits a single client-rendered
		// index.html, which is exactly what the iframe needs.
		router: {
			type: 'hash'
		}
	}
};

export default config;
