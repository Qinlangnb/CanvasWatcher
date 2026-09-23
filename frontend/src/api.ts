export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
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
