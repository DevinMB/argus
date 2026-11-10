import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, proxy the API to a running Argus (ARGUS_URL, default one on
// this machine). Argus serves HTTPS with its own certificate, so it is not verified.
const target = { target: process.env.ARGUS_URL || "https://localhost:8080", secure: false };

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": target, "/metrics": target } },
  build: { outDir: "dist", assetsInlineLimit: 0 },
});
