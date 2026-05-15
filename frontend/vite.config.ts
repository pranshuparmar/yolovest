import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  build: {
    outDir: "dist",
    // Split heavy / commonly-not-needed-on-load vendor packages into
    // their own chunks so the main bundle stays under the 500 KB
    // warning threshold and the cache reuses the chart code across
    // page navigations.
    rollupOptions: {
      output: {
        manualChunks: {
          recharts: ["recharts"],
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          "tanstack-query": ["@tanstack/react-query"],
        },
      },
    },
  },
});
