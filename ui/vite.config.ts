import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the UI runs on its own port and proxies the API to the control plane.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": { target: process.env.AIOS_API ?? "http://127.0.0.1:8600", changeOrigin: true } } },
  build: { outDir: "dist", sourcemap: false },
});
