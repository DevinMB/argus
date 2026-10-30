import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, proxy the API to a running Argus (ARGUS_URL, default the live one).
const target = process.env.ARGUS_URL || "http://192.168.10.18:8080";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": target, "/metrics": target } },
  build: { outDir: "dist", assetsInlineLimit: 0 },
});
