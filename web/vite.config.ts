import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const backend = process.env.STIGMERGY_DEV_BACKEND ?? "http://127.0.0.1:8765";
// An operator's landing page is not part of the platform: it is built only when its sources were
// placed in the build context (web/landing.html and web/src/landing/).
const landing = fileURLToPath(new URL("./landing.html", import.meta.url));

export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: fileURLToPath(new URL("../src/stigmergy/admin/static", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL("./index.html", import.meta.url)),
        ...(existsSync(landing) ? { landing } : {}),
      },
    },
  },
  server: { proxy: { "/admin/api": { target: backend, changeOrigin: false } } },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
