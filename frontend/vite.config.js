import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        timeout: 600_000,
        proxyTimeout: 600_000,
      },
      // Same-origin control in local SPA when VITE_CONTROL_SAME_ORIGIN=1
      "/v1": {
        target: process.env.VITE_CONTROL_PROXY_TARGET || "http://127.0.0.1:8001",
        changeOrigin: true,
      },
    },
  },
});
