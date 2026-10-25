// vite.config.ts

/// <reference types="vitest" />

import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { viteSingleFile } from 'vite-plugin-singlefile';
import { defineConfig } from 'vite';
import { fileURLToPath, URL } from 'node:url';

// NOTE on the single-file settings below (build.outDir, cssCodeSplit,
// assetsInlineLimit, inlineDynamicImports, viteSingleFile) — they mirror the
// React canvas's single-file convention, but under SvelteKit the adapter, NOT
// Vite, owns the final build: SvelteKit overrides build.outDir and
// build.cssCodeSplit and emits the site to `build/` via @sveltejs/adapter-static.
// For this prerendered SPA the adapter produces a SMALL TREE (index.html + an
// `_app/` chunk set), which vite-plugin-singlefile cannot collapse into one
// file. Per design §1.1 "fallback shape", the WHOLE `build/` tree is committed
// under agents/static/agents/svelte-canvas/ and resolves via relative asset
// refs (kit.paths.relative = true). These settings are kept for parity/intent
// and are harmless; the authoritative output path is the adapter's `build/`.
export default defineConfig({
	plugins: [tailwindcss(), sveltekit(), viteSingleFile()],
	optimizeDeps: {
		exclude: ['clsx', '@xyflow/system', 'classcat']
	},
	build: {
		// Intended committed static output. NEVER the React canvas folder.
		// (Overridden by SvelteKit's adapter — see note above.)
		outDir: fileURLToPath(
			new URL('../agents/static/agents/svelte-canvas', import.meta.url)
		),
		emptyOutDir: true,
		// Inline JS/CSS into one index.html (mirrors agent-builder-frontend/vite.config.js).
		assetsInlineLimit: 100000000,
		cssCodeSplit: false,
		rollupOptions: {
			output: {
				inlineDynamicImports: true
			}
		}
	},
	test: {
		environment: 'jsdom',
		include: ['src/**/*.{test,spec}.{js,ts}']
	}
});
