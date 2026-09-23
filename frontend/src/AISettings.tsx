import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { refreshAIConnection, saveAIConnection } from "./aiConnection";

export function AISettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings-ai"], queryFn: () => refreshAIConnection() });
  const initialized = useRef(false);
  const inFlight = useRef(false);
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (query.data && !initialized.current) {
      initialized.current = true;
      setProvider(query.data.provider);
      setModel(query.data.model_id);
    }
  }, [query.data]);
  const selected = query.data?.providers.find(item => item.id === provider);
  const loaded = provider === query.data?.provider && query.data?.credential_loaded;
  const dirty = provider !== query.data?.provider || model.trim() !== query.data?.model_id || !!key.trim();
  const canCall = !!selected && (!selected.requires_key || loaded || !!key.trim());

  async function run(action: "save" | "models" | "test") {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await saveAIConnection({ provider, model, key }, () => setKey(""));
      if (action === "models") {
        const result = await api<{ success: boolean; models: string[]; message: string | null }>(
          "/api/settings/ai/models", { method: "POST" });
        if (!result.success) throw new Error(result.message || "Unable to list models");
        setModels(result.models);
        if (!model.trim() && result.models.length) setModel(result.models[0]);
        setMessage(result.models.length ? `Found ${result.models.length} models. Select one, then test it.` :
          "No models returned. Enter a model ID from your provider, then test it.");
      } else if (action === "test") {
        const result = await api<{ success: boolean; latency_ms: number | null; message: string | null }>(
          `/api/settings/ai/test?model_id=${encodeURIComponent(model.trim())}`, { method: "POST" });
        if (!result.success) throw new Error(result.message || "Connection test failed");
        setMessage(`Connection verified in ${result.latency_ms} ms. AI Chat is ready.`);
      } else {
        setMessage("Connection saved. Fetch models or enter a model ID, then test it to enable AI Chat.");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connection request failed. Please retry.");
    } finally {
      // Refresh AFTER testing, including failures and credential replacement.
      try {
        client.setQueryData(["settings-ai"], await refreshAIConnection());
      } catch {
        setError(previous => previous || "Unable to refresh connection status. Please reload.");
      }
      setBusy(false);
      inFlight.current = false;
    }
  }
  return <section className="settings-panel narrow ai-settings">
    <p className="eyebrow">AI API</p>
    <h2>AI Chat model</h2>
    <p>Select a provider, import its API key, then choose and test a model. AI actions still require your confirmation.</p>
    {query.isError ? <p role="alert">Unable to load AI Settings. <button className="secondary" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</button></p> : null}
    {query.data?.needs_provider_selection ? <p className="form-message">Your previous endpoint is not a supported preset. Select its provider and import the matching key again.</p> : null}
    <fieldset disabled={busy || !query.data} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      <label>Provider / region
        <select value={provider} onChange={event => {
          setProvider(event.target.value); setKey(""); setModel(""); setModels([]); setMessage(""); setError("");
        }}>
          <option value="" disabled>Select provider</option>
          {query.data?.providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
      </label>
      {selected ? <p className="muted" style={{ overflowWrap: "anywhere" }}>Base URL (fixed): {selected.base_url}</p> : null}
      {selected?.requires_key ? <label>API key
        <input type="password" autoComplete="off" value={key} onChange={event => setKey(event.target.value)}
          placeholder={loaded ? "Loaded in backend memory; leave blank to keep" : "Paste this provider's API key"} />
      </label> : selected ? <p>Local Ollama needs no API key. For this Docker deployment use “Docker host”; run Ollama on the host at port 11434 and install a model first.</p> : null}
      <p className="muted">Keys are provider/region-specific and stay in backend memory only. Switching providers removes the old key; restarting the backend requires importing it again. Chat subscriptions and coding-plan keys may not cover this API.</p>
      <label>Model ID
        <input list="ai-model-options" value={model} onChange={event => setModel(event.target.value)} placeholder="Fetch models or enter an exact model ID" />
        <datalist id="ai-model-options">{models.map(value => <option key={value} value={value} />)}</datalist>
      </label>
      <div className="card-actions">
        <button className="primary" disabled={!selected || !dirty} onClick={() => void run("save")}>Save connection</button>
        <button className="secondary" disabled={!canCall} onClick={() => void run("models")}>Fetch models</button>
        <button className="secondary" disabled={!canCall || !model.trim()} onClick={() => void run("test")}>Test selected model</button>
      </div>
    </fieldset>
    {busy ? <p role="status">Connecting…</p> : null}
    {error ? <p role="alert" className="form-message error">{error}</p> : null}
    {message ? <p role="status" className="form-message">{message}</p> : null}
    <small>{!dirty && query.data?.chat_ready ? "AI Chat is ready." :
      "AI Chat remains unavailable for this draft until the selected model passes its connection test."}</small>
  </section>;
}
