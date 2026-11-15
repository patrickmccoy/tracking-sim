import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the UI on :5173 and passes /api to `tracking-sim serve`
// on :8000 (or TRACKING_SIM_API). `npm run build` writes dist/, which serve
// then serves itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": process.env.TRACKING_SIM_API ?? "http://127.0.0.1:8000" },
  },
  worker: { format: "es" },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
});
