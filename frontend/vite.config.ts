import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-only proxy so the frontend can call /products, /waiting-room, /checkout
// against the real FastAPI backend (Caddy on :8000) without a CORS dance.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/products": "http://localhost:8000",
      "/waiting-room": "http://localhost:8000",
      "/checkout": "http://localhost:8000",
      "/orders": "http://localhost:8000",
    },
  },
});
