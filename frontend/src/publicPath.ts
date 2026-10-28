const viteBaseUrl = (import.meta as ImportMeta & { env?: { BASE_URL?: string } })
  .env?.BASE_URL ?? "/";

export function normalizeBasePath(value: string | undefined): string {
  const base = value?.trim() ?? "";
  if (!base || base === "/") return "";
  if (!base.startsWith("/") || /[?#]/.test(base)) {
    throw new Error("The public base must be an absolute path without a query or fragment.");
  }

  const normalized = base.replace(/\/+$/, "");
  const segments = normalized.slice(1).split("/");
  if (!normalized || segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new Error("The public base must not contain empty or traversal path segments.");
  }
  return normalized;
}

export const PUBLIC_BASE_PATH = normalizeBasePath(viteBaseUrl);

export function withBasePath(path: string, basePath = PUBLIC_BASE_PATH): string {
  const base = normalizeBasePath(basePath);
  const suffixIndex = path.search(/[?#]/);
  const pathname = suffixIndex < 0 ? path : path.slice(0, suffixIndex);
  if (!pathname.startsWith("/") || pathname.startsWith("//") || !base) return path;
  if (pathname === base || pathname.startsWith(`${base}/`)) return path;
  return `${base}${path}`;
}

export function toInternalPath(pathname: string, basePath = PUBLIC_BASE_PATH): string {
  const base = normalizeBasePath(basePath);
  if (!pathname) return "/";
  if (!base) return pathname;
  if (pathname === base || pathname === `${base}/`) return "/";
  return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
}
