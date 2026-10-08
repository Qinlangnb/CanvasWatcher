import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, json } from "./api";
import { brokerMessage } from "./brokerMessages";
import { brokerReady, launchBroker, BROKER_START_POLL_MS, BROKER_START_TIMEOUT_MS } from "./brokerLaunch";

type Ticket = { challenge_id: string; instance_id: string; provider: string; one_time_token: string; control_token: string };
const request = (body: unknown): RequestInit => ({ ...json("POST", body), headers: { "Content-Type": "application/json", "X-AW-Broker": "1" } });
const identity = (ticket: Ticket) => ({ challenge_id: ticket.challenge_id, instance_id: ticket.instance_id, provider: ticket.provider });
const brokerTicket = (ticket: Ticket) => ({ ...identity(ticket), one_time_token: ticket.one_time_token });
const control = (ticket: Ticket) => ({ ...identity(ticket), control_token: ticket.control_token });

function useBroker() {
  const config = useQuery({ queryKey: ["auth-broker-config"], queryFn: () => api<{ broker_url: string }>("/api/auth/broker/configuration") });
  const url = config.data?.broker_url;
  const health = useQuery({ queryKey: ["auth-broker-health", url], enabled: !!url, retry: false, refetchInterval: 15000,
    queryFn: () => api<{ protocol_version: number; backend_ready: boolean }>(`${url}/health`, { signal: AbortSignal.timeout(2500) }) });
  return { url, ready: brokerReady(health.data) && !health.isError, health };
}

export function AuthBrokerHealth() {
  const { url, ready, health } = useBroker();
  const [launching, setLaunching] = useState(false);
  const [message, setMessage] = useState("");
  const [launchError, setLaunchError] = useState(false);
  const gate = useRef(false);
  const generation = useRef(0);
  const alive = useRef(true);
  const waitTimer = useRef<number>();
  const wake = useRef<(() => void) | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false; generation.current += 1;
      if (waitTimer.current !== undefined) window.clearTimeout(waitTimer.current);
      wake.current?.();
    };
  }, []);
  async function startBroker() {
    if (gate.current) return;
    gate.current = true;
    const attempt = ++generation.current;
    setLaunching(true); setLaunchError(false);
    setMessage("Confirm the browser prompt to open the installed local launcher. Waiting for the broker and callback connection…");
    try {
      launchBroker();
      const deadline = Date.now() + BROKER_START_TIMEOUT_MS;
      while (alive.current && generation.current === attempt && Date.now() < deadline) {
        const result = await health.refetch();
        if (!alive.current || generation.current !== attempt) return;
        if (!result.isError && brokerReady(result.data)) {
          setMessage("Broker and callback connection are ready.");
          return;
        }
        await new Promise<void>(resolve => {
          wake.current = resolve;
          waitTimer.current = window.setTimeout(() => { wake.current = null; resolve(); }, BROKER_START_POLL_MS);
        });
      }
      if (alive.current && generation.current === attempt) {
        setLaunchError(true);
        setMessage("Broker did not become ready. Confirm the browser prompt and make sure the local launcher is installed on this computer, then retry Start broker.");
      }
    } catch {
      if (alive.current && generation.current === attempt) {
        setLaunchError(true); setMessage("Unable to open the local launcher. Ensure Academic Watcher's launcher is installed on this computer and retry.");
      }
    } finally {
      gate.current = false;
      if (alive.current && generation.current === attempt) setLaunching(false);
    }
  }
  return <section className="settings-panel" aria-label="Local Auth Broker">
    <h3>Local Auth Broker · {ready ? "Running" : "Not running"}</h3>
    {!ready && <p>Browser-based sign-in is unavailable. The broker must run on this computer, outside Docker.</p>}
    <div className="source-action-row">
      <button type="button" className="primary" disabled={!url || launching || ready} onClick={() => void startBroker()}>{launching ? "Starting broker…" : "Start broker"}</button>
      <button type="button" className="secondary" disabled={!url || launching || health.isFetching} onClick={() => void health.refetch()}>Check broker</button>
    </div>
    {message && <p role={launchError ? "alert" : "status"}>{message}</p>}
    <BrowserSignIn credentialId="" clearAll />
  </section>;
}

export function BrowserSignIn({ credentialId, clearAll = false, provider = "canvas" }: { credentialId: string; clearAll?: boolean; provider?: "canvas" | "gradescope" | "prairielearn" }) {
  const { url, ready } = useBroker();
  const client = useQueryClient();
  const ticket = useRef<Ticket | null>(null);
  const starting = useRef(false);
  const intent = useRef(0);
  const alive = useRef(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  async function cancel(value = ticket.current) {
    intent.current += 1;
    if (!value) { if (alive.current) { setBusy(false); setMessage("Sign-in cancelled."); } return; }
    ticket.current = null;
    await Promise.allSettled([
      api("/api/auth/broker/cancel", request(control(value))),
      api(`${url}/cancel`, { ...request(brokerTicket(value)), signal: AbortSignal.timeout(2500) }),
    ]);
    if (alive.current) { setBusy(false); setMessage("Sign-in cancelled."); }
  }
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; void cancel(); };
  }, [url]);
  useEffect(() => {
    if (!busy) return;
    let polling = false;
    const timer = window.setInterval(async () => {
      const value = ticket.current;
      if (!value || polling) return;
      polling = true;
      try {
        const state = await api<{ state: string; reason: string | null }>("/api/auth/broker/status", request(control(value)));
        if (ticket.current !== value || !alive.current) return;
        if (["AUTHENTICATED", "CANCELLED", "FAILED", "TIMED_OUT"].includes(state.state)) {
          ticket.current = null;
          setBusy(false); setError(state.state !== "AUTHENTICATED");
          setMessage(state.state === "AUTHENTICATED" ? "Browser session updated." : brokerMessage(state.reason ?? state.state));
          await Promise.all(["auth-profiles", "canvas-status", "source-connections"].map(key => client.invalidateQueries({ queryKey: [key] })));
        }
      } catch {
        if (alive.current) { setError(true); setMessage("Unable to check sign-in. You can cancel and retry."); }
      } finally { polling = false; }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [busy, client]);
  async function start(operation: "login" | "clear" | "clear_all") {
    if (!ready || starting.current || ticket.current) return;
    if (operation !== "login" && !window.confirm(clearAll ? "Clear all dedicated broker profiles for this Academic Watcher instance? Academic data and API tokens remain." : `Clear the dedicated ${provider} browser profile and session? Course data will remain.`)) return;
    starting.current = true; setBusy(true); setError(false); setMessage("Opening official sign-in…");
    const attempt = ++intent.current;
    try {
      const value = await api<Ticket>("/api/auth/broker/challenges", request({ provider: clearAll ? "all" : provider, credential_id: credentialId, operation }));
      ticket.current = value;
      if (!alive.current || intent.current !== attempt) { await cancel(value); return; }
      await api(`${url}/login`, { ...request(brokerTicket(value)), signal: AbortSignal.timeout(10000) });
      if (alive.current && intent.current === attempt && ticket.current === value) setMessage(operation !== "login" ? "Clearing dedicated browser sessions…" : "Complete sign-in in the browser window. Academic Watcher never receives your password.");
    } catch {
      if (intent.current === attempt) {
        await cancel();
        if (alive.current) { setBusy(false); setError(true); setMessage("Browser sign-in could not start. Check the Local Auth Broker and retry."); }
      }
    } finally { starting.current = false; }
  }
  return <section className="browser-signin-controls" aria-label={clearAll ? "Broker profile management" : `${provider} browser sign-in`}>
    <div className="source-action-row">
    {!clearAll && <button type="button" className="primary" disabled={!ready || busy} onClick={() => void start("login")}>Sign in with Browser</button>}{" "}
    <button type="button" className="ghost danger-text" disabled={!ready || busy} onClick={() => void start(clearAll ? "clear_all" : "clear")}>{clearAll ? "Clear all broker sessions" : `Clear ${provider} browser session`}</button>
    {busy && <button type="button" className="secondary" onClick={() => void cancel()}>Cancel sign-in</button>}
    </div>
    {!ready && !clearAll && <p>Use Start broker in the Local Auth Broker panel, then retry sign-in.</p>}
    {message && <p role={error ? "alert" : "status"}>{message}</p>}
  </section>;
}

export function ProviderSourceForm({ provider, done }: { provider: "gradescope" | "prairielearn"; done: () => void }) {
  const [base, setBase] = useState(provider === "gradescope" ? "https://www.gradescope.com" : "https://us.prairielearn.com/pl");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const gate = useRef(false);
  const [canvasCourse, setCanvasCourse] = useState("");
  const courses = useQuery({ queryKey: ["courses"], queryFn: () => api<{ id: number; course_code: string; name: string }[]>("/api/courses"), enabled: provider === "gradescope" });
  return <form onSubmit={async event => {
    event.preventDefault(); if (gate.current) return;
    gate.current = true; setBusy(true); setError("");
    try { await api("/api/provider-sources", request({ provider, base_url: base, canvas_course_id: canvasCourse ? Number(canvasCourse) : null })); done(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to add source."); }
    finally { gate.current = false; setBusy(false); }
  }}>
    <label>Provider instance URL<input type="url" required value={base} onChange={event => setBase(event.target.value)} disabled={busy} /></label>
    {provider === "gradescope" && <label>Optional Canvas course for LTI sign-in<select value={canvasCourse} onChange={event => setCanvasCourse(event.target.value)} disabled={busy}>
      <option value="">Direct Gradescope / school SSO</option>
      {courses.data?.map(course => <option key={course.id} value={course.id}>{course.course_code} · {course.name}</option>)}
    </select></label>}
    <p>Read-only integration. After saving, sign in through the Local Auth Broker; passwords and MFA stay on the official website.</p>
    {provider === "prairielearn" && <p>Student assessment parsing is not yet verified with an enrolled course.</p>}
    <button className="primary" type="submit" disabled={busy}>{busy ? "Saving…" : "Add source"}</button>
    {error && <p role="alert">{error}</p>}
  </form>;
}

export function CanvasOAuthSignIn({ credentialId }: { credentialId: string }) {
  const capability = useQuery({ queryKey: ["canvas-oauth-capability"],
    queryFn: () => api<{ configured: boolean }>("/api/auth/canvas/oauth/capability") });
  const gate = useRef(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function authorize() {
    if (gate.current) return;
    gate.current = true; setBusy(true); setMessage("");
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    try {
      if (!popup) throw new Error("Allow the sign-in popup and retry.");
      const result = await api<{ authorization_url: string }>("/api/auth/canvas/oauth/start", request({ credential_id: credentialId }));
      popup.location.href = result.authorization_url;
      setMessage("Complete Canvas authorization in the new window, then refresh credential status.");
    } catch (reason) {
      popup?.close(); setMessage(reason instanceof Error ? reason.message : "Canvas authorization unavailable.");
    } finally { gate.current = false; setBusy(false); }
  }
  return <section aria-label="Canvas OAuth">
    {capability.data?.configured ? <button type="button" className="primary" disabled={busy} onClick={() => void authorize()}>Authorize with Canvas OAuth</button>
      : <p>Canvas OAuth is not configured by this installation. Browser sign-in and advanced PAT remain available.</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
