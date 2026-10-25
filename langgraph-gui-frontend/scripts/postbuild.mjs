// Postbuild: make the hash-routed adapter-static output servable from Django's
// deep /static/agents/svelte-canvas/ sub-path inside an iframe, then publish it.
//
// Two reasons this script exists:
//  1. RELATIVE ASSET REFS. Under kit.router.type='hash' the adapter emits a
//     `fallback` index.html whose asset refs are base-ROOTED ABSOLUTE
//     (`/_app/...`). Served from /static/agents/svelte-canvas/index.html those
//     resolve to the SITE ROOT (/_app/...) and 404. kit.paths.relative does NOT
//     apply to the fallback page, so we rewrite `"/_app/` -> `"./_app/` (and the
//     dynamic-import `("/_app/` -> `("./_app/`) to make every ref relative to the
//     page, which resolves correctly under the static sub-path.
//  2. PUBLISH. SvelteKit's adapter owns the output dir and always writes to
//     `build/` (it overrides vite build.outDir). The committed, Django-served
//     copy lives at ../agents/static/agents/svelte-canvas/, so we sync build/ ->
//     there after rewriting. This replaces the manual copy the build would
//     otherwise need and keeps a plain `npm run build` correct and repeatable.
//
// Hash routing itself (not a prerendered '/') is what lets the SPA load from an
// arbitrary path without SvelteKit's client router rendering its built-in 404:
// the router reads the URL fragment, never the deep pathname.

import { readFileSync, writeFileSync, rmSync, mkdirSync, cpSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';

const buildDir = fileURLToPath(new URL('../build', import.meta.url));
const indexHtml = `${buildDir}/index.html`;
const staticDir = fileURLToPath(
	new URL('../../agents/static/agents/svelte-canvas', import.meta.url)
);

// 1. Rewrite absolute _app refs to relative in the fallback shell.
let html = readFileSync(indexHtml, 'utf8');
const before = html;
html = html.replaceAll('"/_app/', '"./_app/').replaceAll("('/_app/", "('./_app/");
if (html !== before) {
	writeFileSync(indexHtml, html);
	console.log('[postbuild] rewrote absolute /_app/ refs -> relative ./_app/ in build/index.html');
} else {
	console.log('[postbuild] no absolute /_app/ refs found (nothing to rewrite)');
}
// Guard: fail loudly if any absolute _app ref survived (would 404 under Django).
if (/["(]\/_app\//.test(readFileSync(indexHtml, 'utf8'))) {
	console.error('[postbuild] ERROR: absolute /_app/ refs remain in build/index.html');
	process.exit(1);
}

// 2. Publish build/ -> the committed, Django-served static folder.
rmSync(staticDir, { recursive: true, force: true });
mkdirSync(staticDir, { recursive: true });
cpSync(buildDir, staticDir, { recursive: true });
console.log(`[postbuild] published build/ -> ${staticDir}`);
