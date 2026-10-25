// vite.config.ts

/// <reference types="vitest" />

import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// Build/output is owned by SvelteKit's @sveltejs/adapter-static: it emits the
// prerendered SPA (a fallback index.html + an `_app/` chunk set) to `build/`,
// which the postbuild step rewrites to relative asset refs and publishes to
// agents/static/agents/svelte-canvas/.
//
// HISTORY: this file previously mirrored the React canvas's single-file
// convention (viteSingleFile() + cssCodeSplit=false + assetsInlineLimit=huge +
// inlineDynamicImports=true). Under @sveltejs/adapter-static with hash routing
// those overrides fought the adapter: the emitted CSS asset was orphaned and
// the fallback index.html linked ZERO stylesheets, so the served canvas
// rendered completely unstyled. They are removed so SvelteKit/Vite bundle and
// reference the CSS normally; postbuild.mjs then guarantees the fallback
// index.html links the emitted stylesheet relatively.
export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	optimizeDeps: {
		exclude: ['clsx', '@xyflow/system', 'classcat']
	},
	test: {
		// FEAT-002's logic modules (api/adapter/run) are pure, DOM-free TS
		// (design §8), so they run under the lightweight node environment.
		// Any future DOM-coupled component tests can opt into jsdom per-file via
		// a `// @vitest-environment jsdom` docblock.
		environment: 'node',
		include: ['src/**/*.{test,spec}.{js,ts}']
	}
});
