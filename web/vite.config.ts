import { defineConfig } from "vite"
import solid from "vite-plugin-solid"
import tailwindcss from "@tailwindcss/vite"
import path from "path"

const hub = "http://127.0.0.1:8787"

export default defineConfig({
  plugins: [
    // HMR is disabled in test transforms so Vitest can run JSX in jsdom without
    // resolving the browser-only solid-refresh virtual module.
    solid({ hot: false }),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 3001,
    proxy: {
      "/api/": { target: hub, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
  },
})
