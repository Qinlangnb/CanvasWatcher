import { withBasePath } from "./publicPath";

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function readCsrfCookie(cookie: string): string | null {
  for (const entry of cookie.split(";")) {
    const separator = entry.indexOf("=");
    if (separator < 0 || entry.slice(0, separator).trim() !== "aw_csrf") continue;
    const value = entry.slice(separator + 1).trim();
    try {
      return decodeURIComponent(value);
    } catch {
      return value;
    }
  }
  return null;
}

function isSameOriginRequest(path: string): boolean {
  if (typeof location === "undefined") return path.startsWith("/") && !path.startsWith("//");
  try {
    return new URL(path, location.href).origin === location.origin;
  } catch {
    return false;
  }
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (isSameOriginRequest(path) && MUTATING_METHODS.has(method) && typeof document !== "undefined") {
    const token = readCsrfCookie(document.cookie);
    if (token) headers.set("X-AW-CSRF", token);
  }
  return fetch(withBasePath(path), { ...init, headers });
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const validationMessage = Array.isArray(detail)
      ? detail.flatMap((item: unknown) => item && typeof item === "object" && "msg" in item && typeof item.msg === "string" ? [item.msg] : []).join("; ")
      : "";
    throw new Error(
      typeof detail === "string"
        ? detail
        : (validationMessage || (detail?.message ??
          body?.message ??
          `Request failed (${response.status})`)),
    );
  }
  return body;
}

export const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
