import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { fileURLToPath, URL } from "node:url";

// The canvas is embedded as a same-origin <iframe> in the Django
// /agent-builder/ page. We build to a SINGLE self-contained index.html
// (JS + CSS inlined) written into agents/static/agents/canvas/ so Django's
// {% static 'agents/canvas/index.html' %} + collectstatic serve it with no
// asset-path rewriting and no extra files.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  // Relative base so the single bundle has no absolute asset URLs.
  base: "./",
  build: {
    outDir: fileURLToPath(
      new URL("../agents/static/agents/canvas", import.meta.url)
    ),
    emptyOutDir: true,
    // Inline everything into one HTML file.
    assetsInlineLimit: 100000000,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
