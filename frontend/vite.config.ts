import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { normalizeBasePath } from "./src/publicPath";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  const publicBasePath = normalizeBasePath(env.VITE_PUBLIC_BASE);
  const viteBase = publicBasePath ? `${publicBasePath}/` : "/";
  const apiPrefix = publicBasePath ? `${publicBasePath}/api` : "/api";
  const proxy = {
    target: env.VITE_API_PROXY || "http://backend:8000",
    ...(publicBasePath ? { rewrite: (path: string) => path.slice(publicBasePath.length) } : {}),
  };
  return {
    base: viteBase,
    plugins: [react()],
    server: { proxy: { [apiPrefix]: proxy } },
  };
});
