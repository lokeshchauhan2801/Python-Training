// Static client-only SPA served by Django from a static file inside an iframe.
// Routing is hash-based (see svelte.config.js kit.router.type = 'hash'), which
// is inherently client-side-only, so page-level `prerender`/`ssr` options are
// neither needed nor allowed here — SvelteKit rejects them under hash routing.
// SSR is off by construction; the adapter emits a single client-rendered
// index.html. Nothing to export.
export {};
