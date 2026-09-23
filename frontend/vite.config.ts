import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  return {plugins: [react()], server: {proxy: {"/api": env.VITE_API_PROXY || "http://backend:8000"}}};
});
