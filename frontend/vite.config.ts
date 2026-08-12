import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// If VITE_API_URL is set, requests hit the real backend directly (see
// src/api/client.ts). The optional dev proxy below lets you instead call the
// backend on the same origin under /api during `npm run dev` to sidestep CORS —
// point it at wherever you host the FastAPI/Flask wrapper.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
