import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { fileURLToPath, URL } from "node:url";


export default defineConfig({
  plugins: [react(), viteSingleFile()],

  base: "./",
  build: {
    outDir: fileURLToPath(
      new URL("../agents/static/agents/canvas", import.meta.url)
    ),
    emptyOutDir: true,

    assetsInlineLimit: 100000000,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
