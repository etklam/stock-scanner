/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The compiled app is served by qscan serve under /ui/ (same origin as the
// API). Dev mode uses the Vite proxy against a locally running
// `qscan serve --dev-origin http://localhost:5173` — the explicit dev origin
// flag is what makes requests pass; the proxy never strips Origin.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000" },
      "/health": { target: "http://127.0.0.1:8000" },
    },
  },
  build: {
    // Output lands straight inside the Python package so the wheel ships the
    // compiled UI and `qscan serve` needs no Node at runtime.
    outDir: "../src/qscan/interfaces/web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    // e2e/ holds Playwright browser tests (`npm run test:e2e`), not vitest.
    exclude: ["**/node_modules/**", "e2e/**"],
  },
});
