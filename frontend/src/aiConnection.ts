import { api, json } from "./api";
import type { AIProbeSettings } from "./types";

export type AIConnectionDraft = { provider: string; model: string; key: string };

export async function saveAIConnection(draft: AIConnectionDraft, keySaved: () => void, request = api) {
  await request("/api/settings/ai", json("PUT", { provider: draft.provider, model_id: draft.model.trim() }));
  if (draft.key.trim()) {
    await request("/api/settings/ai/credential", json("POST", { provider: draft.provider, api_key: draft.key.trim() }));
    keySaved();
  }
}

export async function refreshAIConnection(request = api): Promise<AIProbeSettings> {
  return request<AIProbeSettings>("/api/settings/ai");
}
