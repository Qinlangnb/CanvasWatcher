import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, json } from "./api";
import { AISettings } from "./AISettings";
import { ChangeFilters } from "./ChangeFilters";
import { ActionFeedback, useAsyncAction } from "./ActionFeedback";
import { FilePickerButton } from "./FilePickerButton";
import { AuthBrokerHealth, BrowserSignIn, CanvasOAuthSignIn, ProviderSourceForm } from "./AuthBroker";
import { CommitButton } from "./CommitButton";
import { SourceStatusBadge } from "./SourceStatusBadge";
import { CanvasCredentialSlots } from "./CanvasCredentialSlots";
import { ProviderCourses } from "./ProviderCourses";
import { VisualCalendar, type EventDraft } from "./VisualCalendar";
import { DateTime } from "luxon";
import type {
  AuthProfile,
  AvailabilityOverride,
  AvailabilityRule,
  CalendarEvent,
  CanvasStatus,
  Course,
  CourseTerm,
  ICSImportSource,
  ICSPreview,
  PendingResource,
  SourceConnection,
} from "./types";
import { formatUiucDateTime } from "./timezone";

type SettingsPage = "general" | "sources" | "calendar" | "ai";
const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

export function SettingsView({
  page,
  search,
  navigate,
  courses,
  status,
  profiles,
  pending,
}: {
  page: SettingsPage;
  search: string;
  navigate: (path: string, replace?: boolean) => void;
  courses: Course[];
  status: CanvasStatus | undefined;
  profiles: AuthProfile[];
  pending: PendingResource[];
}) {
  return (
    <>
      <div
        className="settings-tabs"
        role="navigation"
        aria-label="Settings sections"
      >
        {(["general", "sources", "calendar", "ai"] as const).map((value) => (
          <button
            key={value}
            className={page === value ? "active" : ""}
            onClick={() => navigate(`/settings/${value}`)}
          >
            {value === "ai"
              ? "AI API"
              : value[0].toUpperCase() + value.slice(1)}
          </button>
        ))}
      </div>
      {page === "general" ? (
        <GeneralSettings />
      ) : page === "sources" ? (
        <SourcesSettings
          search={search}
          navigate={navigate}
          status={status}
          profiles={profiles}
          pending={pending}
        />
      ) : page === "calendar" ? (
        <CalendarSettings courses={courses} />
      ) : (
        <><AISettings /><ChangeFilters /></>
      )}
    </>
  );
}

function GeneralSettings() {
  const query = useQuery({
    queryKey: ["settings-general"],
    queryFn: () => api<{ academic_timezone: string }>("/api/settings/general"),
  });
  const [value, setValue] = useState("America/Chicago");
  const [saved, setSaved] = useState("America/Chicago");
  const [error, setError] = useState("");
  useEffect(() => {
    if (query.data) {
      setValue(query.data.academic_timezone);
      setSaved(query.data.academic_timezone);
    }
  }, [query.data]);
  return (
    <section className="settings-panel narrow">
      <p className="eyebrow">GENERAL</p>
      <h2>Academic time</h2>
      <p>
        Dates and study-capacity calculations use an explicit IANA timezone.
      </p>
      <label>
        Academic timezone
        <input
          value={value}
          onChange={(event) => { setValue(event.target.value); setError(""); }}
        />
      </label>
      <CommitButton
        dirty={value !== saved}
        onCommit={async () => {
          setError("");
          try {
            await api(
              "/api/settings/general",
              json("PUT", { academic_timezone: value }),
            );
            setSaved(value);
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "Unable to save timezone");
            throw reason;
          }
        }}
      />
      {error && <p role="alert" className="form-message error">{error}</p>}
    </section>
  );
}

function SourcesSettings({
  search,
  navigate,
  status,
  profiles,
  pending,
}: {
  search: string;
  navigate: (path: string, replace?: boolean) => void;
  status: CanvasStatus | undefined;
  profiles: AuthProfile[];
  pending: PendingResource[];
}) {
  const queryClient = useQueryClient();
  const connections = useQuery({
    queryKey: ["source-connections"],
    queryFn: () => api<SourceConnection[]>("/api/source-connections"),
  });
  const terms = useQuery({
    queryKey: ["terms"],
    queryFn: () => api<CourseTerm[]>("/api/courses/terms"),
  });
  const [adding, setAdding] = useState(false);
  const [type, setType] = useState<SourceConnection["source_type"] | null>(null);
  const [editing, setEditing] = useState<SourceConnection | null>(null);
  async function refresh() {
    await Promise.all(
      [
        ["source-connections"],
        ["auth-profiles"],
        ["sources"],
        ["canvas-status"],
        ["terms"],
        ["courses"],
      ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
    );
  }
  useEffect(() => {
    const id = Number(new URLSearchParams(search).get("edit"));
    if (id && connections.data) {
      const target = connections.data.find((row) => row.id === id);
      if (!target) return;
      setEditing(target);
      // Consume this navigation intent once; refetches must not reopen a saved
      // or dismissed editor (or overwrite an in-progress form).
      const url = new URL(location.href);
      url.searchParams.delete("edit");
      navigate(`${url.pathname}${url.search}${url.hash}`, true);
    }
  }, [connections.data, search]);
  return (
    <>
      <div className="section-title">
        <div>
          <p className="eyebrow">SOURCES</p>
          <h2>Configured connections</h2>
          <p>
            Edit safely: a changed address is probed before it replaces the
            working configuration.
          </p>
        </div>
        <button
          className="primary"
          onClick={() => {
            setAdding(true);
            setType(null);
          }}
        >
          Add New Source
        </button>
      </div>
      <AuthBrokerHealth />
      {!connections.data?.length ? (
        <div className="empty">
          <h3>No sources configured</h3>
          <p>Add Canvas or a course website when you are ready.</p>
          <button className="primary" onClick={() => setAdding(true)}>
            Add New Source
          </button>
        </div>
      ) : (
        <div className="source-connection-grid">
          {connections.data.map((connection) => (
            <ConnectionCard
              key={connection.id}
              connection={connection}
              profile={profiles.find(
                (row) => row.credential_id === connection.credential_id,
              )}
              status={connection.source_type === "canvas" ? status : undefined}
              pending={
                pending.filter(
                  (row) => row.credential_id === connection.credential_id,
                ).length
              }
              refresh={refresh}
              edit={() => setEditing(connection)}
            />
          ))}
        </div>
      )}
      {adding ? (
        <SourceWizard
          terms={terms.data ?? []}
          type={type}
          setType={setType}
          close={() => setAdding(false)}
          done={async () => {
            await refresh();
            setAdding(false);
          }}
        />
      ) : null}
      {editing ? (
        <SourceEdit
          terms={terms.data ?? []}
          connection={editing}
          close={() => setEditing(null)}
          done={async () => {
            await refresh();
            setEditing(null);
          }}
        />
      ) : null}
    </>
  );
}

function ConnectionCard({
  connection,
  profile,
  status,
  pending,
  refresh,
  edit,
}: {
  connection: SourceConnection;
  profile: AuthProfile | undefined;
  status: CanvasStatus | undefined;
  pending: number;
  refresh: () => void;
  edit: () => void;
}) {
  const [authOpen, setAuthOpen] = useState(false);
  if (connection.source_type === "gradescope" || connection.source_type === "prairielearn") {
    return <article className="source-connection-card">
      <div className="source-card-heading"><div><p className="eyebrow">READ-ONLY SOURCE</p><h3>{connection.name}</h3><p>{connection.base_url}</p></div><SourceStatusBadge state={connection.state} /></div>
      <p>Browser authentication · Session stays in backend memory.</p>
      {connection.credential_id && <BrowserSignIn credentialId={connection.credential_id} provider={connection.source_type} />}
      {connection.source_type === "gradescope" ? <ProviderCourses connectionId={connection.id} /> : <p>Student course parsing awaits representative course data; authentication can be tested independently.</p>}
    </article>;
  }
  return (
    <article className="source-connection-card">
      <div className="source-card-heading">
        <div>
          <p className="eyebrow">{connection.source_type}</p>
          <h3>{connection.name}</h3>
          <p>{connection.base_url}</p>
        </div>
        <SourceStatusBadge
          state={status?.credential_state ?? connection.state}
        />
      </div>
      <p>
        {connection.course_id ? "Course-linked source" : "Account-level source"}
        {pending ? ` · ${pending} protected resources pending` : ""}
      </p>
      <div className="card-actions">
        <button className="secondary" onClick={edit}>
          Edit
        </button>
        {profile ? (
          <button className="secondary" onClick={() => setAuthOpen(!authOpen)}>
            {authOpen ? "Hide authentication" : "Authenticate"}
          </button>
        ) : null}
      </div>
      {authOpen && profile ? (
        <CredentialForm profile={profile} status={status} refresh={refresh} />
      ) : null}
      {connection.source_type === "canvas" && profile && <>
        <BrowserSignIn credentialId={profile.credential_id} />
        <p>Sign in on the official Canvas page. The local broker transfers the session automatically; no Cookie or cURL import is needed.</p>
      </>}
    </article>
  );
}

function SourceWizard({
  type,
  setType,
  close,
  done,
  terms,
}: {
  type: SourceConnection["source_type"] | null;
  setType: (value: SourceConnection["source_type"]) => void;
  close: () => void;
  done: () => void;
  terms: CourseTerm[];
}) {
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <section className="dialog source-wizard" role="dialog" aria-modal="true">
        <div className="dialog-head">
          <div>
            <p className="eyebrow">ADD NEW SOURCE</p>
            <h2>{type ?? "Choose source type"}</h2>
          </div>
          <button className="icon-button" aria-label="Close source wizard" onClick={close}>
            ×
          </button>
        </div>
        {!type ? (
          <div className="source-type-grid">
            <button onClick={() => setType("canvas")}>
              <strong>Canvas</strong>
              <span>PAT or browser-session authentication</span>
            </button>
            <button onClick={() => setType("website")}>
              <strong>Course Website</strong>
              <span>Public or NTLM-protected course site</span>
            </button>
            <button onClick={() => setType("gradescope")}><strong>Gradescope</strong><span>Read-only student assignments and submission facts</span></button>
            <button onClick={() => setType("prairielearn")}><strong>PrairieLearn</strong><span>Configurable student instance · browser sign-in</span></button>
          </div>
        ) : (
          <SourceForm terms={terms} type={type} done={done} />
        )}
      </section>
    </div>
  );
}

function SourceForm(props: { type: SourceConnection["source_type"]; done: () => void; terms: CourseTerm[] }) {
  if (props.type === "gradescope" || props.type === "prairielearn") return <ProviderSourceForm provider={props.type} done={props.done} />;
  return props.type === "website" ? <WebsiteForm terms={props.terms} done={props.done} /> : <CanvasSourceForm type="canvas" terms={props.terms} done={props.done} />;
}

function SourceEdit(props: { connection: SourceConnection; close: () => void; done: () => void; terms: CourseTerm[] }) {
  if (props.connection.source_type !== "website") return <CanvasSourceEdit {...props} />;
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && props.close()}>
    <section className="dialog source-wizard" role="dialog" aria-modal="true" aria-label="Edit course website">
      <div className="dialog-head"><h2>Edit course website</h2><button className="icon-button" aria-label="Close website editor" onClick={props.close}>×</button></div>
      <WebsiteForm key={props.connection.id} connection={props.connection} terms={props.terms} done={props.done} />
    </section>
  </div>;
}

function WebsiteForm({ connection, terms, done }: { connection?: SourceConnection; terms: CourseTerm[]; done: () => void }) {
  const config = connection?.config_json ?? {};
  const legacyRule = (config.auth_rules as { path_prefix?: string; auth?: { type?: string; probe_url?: string } }[] | undefined)?.find(row => row.auth?.type === "ntlm");
  const initial = {
    name: connection?.name ?? "", course_code: String(config.course_code ?? ""),
    course_name: String(config.course_name ?? ""), term: String(config.term ?? (connection ? "" : terms[0]?.display_name) ?? ""),
    base_url: connection?.base_url ?? "", authentication_method: String(config.authentication_method ?? (connection?.credential_id || legacyRule ? "ntlm" : "none")),
    // Match the backend's legacy fallback: only absent keys inherit auth-rule values.
    // An explicit null is an empty setting, not a request to change network scope.
    protected_path_prefix: String(("protected_path_prefix" in config ? config.protected_path_prefix : legacyRule?.path_prefix) ?? ""),
    probe_url: String(("probe_url" in config ? config.probe_url : legacyRule?.auth?.probe_url) ?? ""),
    discovery_path_limit: Number(config.discovery_path_limit ?? 100),
  };
  const [form, setForm] = useState(initial);
  const [termMode, setTermMode] = useState<"existing" | "new">(terms.some(term => term.display_name === initial.term) ? "existing" : "new");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const composing = useRef(false);
  const formRef = useRef<HTMLFormElement>(null);
  const profiles = useQuery({ queryKey: ["auth-profiles"], queryFn: () => api<AuthProfile[]>("/api/auth/profiles") });
  const profile = profiles.data?.find(row => row.credential_id === connection?.credential_id);
  useEffect(() => { if (profile?.username) setUsername(old => old || profile.username || ""); }, [profile?.username]);
  const networkChanged = ["base_url", "authentication_method", "protected_path_prefix", "probe_url"].some(key =>
    form[key as keyof typeof form] !== initial[key as keyof typeof initial]);
  const dirty = !connection || JSON.stringify(form) !== JSON.stringify(initial) || !!password || username !== (profile?.username ?? "");
  const set = (key: keyof typeof form, value: string) => { setForm(old => ({ ...old, [key]: value })); setMessage(""); };
  async function submit(testOnly: boolean) {
    if (busyRef.current || composing.current) return;
    if (!formRef.current?.reportValidity()) throw new Error("Complete the required fields");
    busyRef.current = true;
    setBusy(true); setError(""); setMessage("");
    try {
      const includeLogin = form.authentication_method === "ntlm" && (testOnly || !connection || networkChanged || !!password || username !== (profile?.username ?? ""));
      const payload = { ...form, term: connection && form.term === initial.term ? undefined : form.term,
        name: form.name.trim() || `${form.course_code.trim()} Site`,
        ...(includeLogin ? { ntlm: { username, password } } : {}) };
      if (testOnly) {
        const result = await api<{ verified: boolean; message: string }>(`/api/source-connections/website/test${connection ? `?connection_id=${connection.id}` : ""}`, json("POST", payload));
        setMessage(result.message);
      } else {
        await api(connection ? `/api/source-connections/${connection.id}` : "/api/source-connections/website", json(connection ? "PUT" : "POST", payload));
        setPassword("");
        done();
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Website test failed");
      throw reason;
    } finally { busyRef.current = false; setBusy(false); }
  }
  return <form ref={formRef} className="form-grid" onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
    onKeyDown={event => {
      if (event.key !== "Enter") return;
      if (event.nativeEvent.isComposing || composing.current) { event.preventDefault(); return; }
      if (event.target instanceof HTMLInputElement) {
        event.preventDefault();
        if (dirty) formRef.current?.requestSubmit();
      }
    }}
    onSubmit={event => { event.preventDefault(); if (dirty) void submit(false).catch(() => {}); }}>
    <label>Subject / Course code<input autoFocus required placeholder="PHYS 225 or ASTR 405" value={form.course_code} onChange={event => set("course_code", event.target.value)} /></label>
    <label>Description<input required placeholder="Short course title or description" value={form.course_name} onChange={event => set("course_name", event.target.value)} /></label>
    <TermPicker terms={terms} mode={termMode} value={form.term} setMode={setTermMode} setValue={value => set("term", value)} allowUnchangedEmpty={!!connection && !initial.term && !form.term} />
    <label>Course website URL<input required type="url" placeholder="https://courses.example.edu/course/" value={form.base_url} onChange={event => set("base_url", event.target.value)} /></label>
    <label>Authentication<select value={form.authentication_method} onChange={event => { set("authentication_method", event.target.value); setPassword(""); }}>
      <option value="none">Public</option><option value="ntlm">NTLM (username and password)</option>
    </select></label>
    {form.authentication_method === "ntlm" && <>
      <label>Username<input autoComplete="username" required={!connection || !!password} placeholder="Your website account (domain\\username if required)" value={username} onChange={event => setUsername(event.target.value)} /></label>
      <label>Password<input type="password" autoComplete="off" required={!connection} value={password} onChange={event => setPassword(event.target.value)} /></label>
      <small>Kept only in backend memory. {connection ? "Leave blank to keep a matching loaded password; after restart, enter it again for protected access. Label-only edits do not require a password." : "Public pages alone do not verify protected access."}</small>
      {profile?.last_error_code === "protected_access_not_verified" && <p>Public website reachable. Protected access not yet verified.</p>}
      <details><summary>Advanced access settings</summary>
        <label>Protected resource path (optional, usually detected)<input placeholder="Leave empty to use the course website path" value={form.protected_path_prefix} onChange={event => set("protected_path_prefix", event.target.value)} /></label>
        <label>Protected test page (optional)<input type="url" value={form.probe_url} onChange={event => set("probe_url", event.target.value)} /></label>
        <small>Existing narrow scopes are retained. Credentials are never sent outside this source's trusted path and origin.</small>
      </details>
    </>}
    <details><summary>Advanced source settings</summary><label>Source label<input placeholder={`${form.course_code || "Course"} Site`} value={form.name} onChange={event => set("name", event.target.value)} /></label></details>
    <p>Test connection does not save. Saving changed access settings verifies them again before replacing the working configuration.</p>
    <div className="dialog-actions"><button type="button" className="secondary" disabled={busy} onClick={() => void submit(true).catch(() => {})}>{busy ? "Checking…" : "Test connection"}</button>
      <CommitButton dirty={dirty} disabled={busy} label="Save website" onCommit={() => submit(false)} /></div>
    {message && <p role="status" className="form-message">{message}</p>}
    {error && <p role="alert" className="form-message error">{error}</p>}
  </form>;
}

function CanvasSourceForm({
  type,
  done,
  terms,
}: {
  type: "canvas" | "website";
  done: () => void;
  terms: CourseTerm[];
}) {
  const [form, setForm] = useState({
    name: "Canvas — University of Illinois",
    course_name: "",
    course_code: "",
    term: terms[0]?.display_name ?? "",
    base_url: "",
    authentication_method: "none",
    protected_path_prefix: "",
    probe_url: "",
  });
  const [termMode, setTermMode] = useState<"existing" | "new">(
    terms.length ? "existing" : "new",
  );
  const createAction = useAsyncAction();
  const set = (key: string, value: string) =>
    setForm((old) => ({ ...old, [key]: value }));
  return (
    <form
      className="form-grid"
      onSubmit={async (event) => {
        event.preventDefault();
        void createAction.run("Create source", async () => {
          await api(
            `/api/source-connections/${type}`,
            json(
              "POST",
              type === "canvas"
                ? { name: form.name, base_url: form.base_url }
                : form,
            ),
          );
          done();
        });
      }}
    >
      {type === "canvas" ? (
        <label>
          Name
          <input
            value={form.name}
            onChange={(event) => set("name", event.target.value)}
          />
        </label>
      ) : (
        <>
          <label>
            Course name
            <input
              required
              value={form.course_name}
              onChange={(event) => set("course_name", event.target.value)}
            />
          </label>
          <label>
            Course code
            <input
              value={form.course_code}
              onChange={(event) => set("course_code", event.target.value)}
            />
          </label>
          <TermPicker
            terms={terms}
            mode={termMode}
            value={form.term}
            setMode={setTermMode}
            setValue={(value) => set("term", value)}
          />
        </>
      )}
      <label>
        Base URL
        <input
          type="url"
          required
          value={form.base_url}
          onChange={(event) => set("base_url", event.target.value)}
        />
      </label>
      {type === "website" ? (
        <>
          <label>
            Authentication
            <select
              value={form.authentication_method}
              onChange={(event) =>
                set("authentication_method", event.target.value)
              }
            >
              <option value="none">Public</option>
              <option value="ntlm">NTLM</option>
            </select>
          </label>
          {form.authentication_method === "ntlm" ? (
            <>
              <label>
                Protected path prefix
                <input
                  required
                  placeholder="/protected/course/"
                  value={form.protected_path_prefix}
                  onChange={(event) =>
                    set("protected_path_prefix", event.target.value)
                  }
                />
              </label>
              <label>
                Probe URL
                <input
                  type="url"
                  required
                  value={form.probe_url}
                  onChange={(event) => set("probe_url", event.target.value)}
                />
              </label>
            </>
          ) : null}
        </>
      ) : null}
      <button className="primary" disabled={createAction.busy} aria-busy={createAction.busy}>{createAction.busy ? "Creating…" : "Create source"}</button>
      <ActionFeedback action={createAction} />
    </form>
  );
}

function CanvasSourceEdit({
  connection,
  close,
  done,
  terms,
}: {
  connection: SourceConnection;
  close: () => void;
  done: () => void;
  terms: CourseTerm[];
}) {
  const config = connection.config_json ?? {};
  const initial = {
    name: connection.name,
    base_url: connection.base_url ?? "",
    course_name: String(config.course_name ?? connection.name),
    course_code: String(config.course_code ?? ""),
    term: String(config.term ?? ""),
    authentication_method: String(config.authentication_method ?? "none"),
    protected_path_prefix: String(config.protected_path_prefix ?? ""),
    probe_url: String(config.probe_url ?? connection.base_url ?? ""),
    default_auth_method: String(config.default_auth_method ?? "pat"),
    discovery_path_limit: Number(config.discovery_path_limit ?? 100),
  };
  const [form, setForm] = useState(initial);
  const [termMode, setTermMode] = useState<"existing" | "new">(
    terms.some((row) => row.display_name === initial.term) ? "existing" : "new",
  );
  const [error, setError] = useState("");
  const dirty = JSON.stringify(form) !== JSON.stringify(initial);
  const commit = async () => {
    try {
      setError("");
      await api(`/api/source-connections/${connection.id}`, json("PUT", form));
      done();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Probe failed");
      throw reason;
    }
  };
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <section className="dialog source-wizard">
        <div className="dialog-head">
          <div>
            <p className="eyebrow">EDIT SOURCE</p>
            <h2>{connection.name}</h2>
          </div>
          <button className="icon-button" aria-label="Close source editor" onClick={close}>
            ×
          </button>
        </div>
        <form
          className="form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            if (dirty) void commit();
          }}
        >
          <label>
            Name
            <input
              value={form.name}
              onChange={(event) =>
                setForm((old) => ({ ...old, name: event.target.value }))
              }
            />
          </label>
          <label>
            Base URL
            <input
              type="url"
              value={form.base_url}
              onChange={(event) =>
                setForm((old) => ({ ...old, base_url: event.target.value }))
              }
            />
          </label>
          {connection.source_type === "website" ? (
            <>
              <label>
                Course name
                <input
                  value={form.course_name}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      course_name: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Course code
                <input
                  value={form.course_code}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      course_code: event.target.value,
                    }))
                  }
                />
              </label>
              <TermPicker
                terms={terms}
                mode={termMode}
                value={form.term}
                setMode={setTermMode}
                setValue={(value) =>
                  setForm((old) => ({ ...old, term: value }))
                }
              />
              <label>
                Authentication
                <select
                  value={form.authentication_method}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      authentication_method: event.target.value,
                    }))
                  }
                >
                  <option value="none">Public</option>
                  <option value="ntlm">NTLM</option>
                </select>
              </label>
              {form.authentication_method === "ntlm" ? (
                <>
                  <label>
                    Protected path prefix
                    <input
                      required
                      placeholder="/protected/course/"
                      value={form.protected_path_prefix}
                      onChange={(event) =>
                        setForm((old) => ({
                          ...old,
                          protected_path_prefix: event.target.value,
                        }))
                      }
                    />
                  </label>
                  <label>
                    Probe URL
                    <input
                      required
                      type="url"
                      value={form.probe_url}
                      onChange={(event) =>
                        setForm((old) => ({
                          ...old,
                          probe_url: event.target.value,
                        }))
                      }
                    />
                  </label>
                </>
              ) : null}
            </>
          ) : (
            <label>
              Default authentication
              <select
                value={form.default_auth_method}
                onChange={(event) =>
                  setForm((old) => ({
                    ...old,
                    default_auth_method: event.target.value,
                  }))
                }
              >
                <option value="pat">PAT</option>
                <option value="browser_session">Browser session</option>
              </select>
            </label>
          )}
          <p>
            Source identity and history are preserved. If the probe fails, the
            existing working URL remains active.
          </p>
          <CommitButton dirty={dirty} label="Save source" onCommit={commit} />
          {error ? <p role="alert" className="form-message error">{error}</p> : null}
        </form>
      </section>
    </div>
  );
}

function TermPicker({
  terms,
  mode,
  value,
  setMode,
  setValue,
  allowUnchangedEmpty = false,
}: {
  terms: CourseTerm[];
  mode: "existing" | "new";
  value: string;
  setMode: (value: "existing" | "new") => void;
  setValue: (value: string) => void;
  allowUnchangedEmpty?: boolean;
}) {
  return (
    <fieldset className="term-picker">
      <legend>Term</legend>
      <label className="inline-check">
        <input
          type="radio"
          checked={mode === "existing"}
          disabled={!terms.length}
          onChange={() => {
            setMode("existing");
            setValue(terms[0]?.display_name ?? "");
          }}
        />
        Use existing term
      </label>
      {mode === "existing" ? (
        <select
          aria-label="Existing term"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        >
          {terms.map((term) => (
            <option key={term.term_id} value={term.display_name}>
              {term.display_name}
            </option>
          ))}
        </select>
      ) : null}
      <label className="inline-check">
        <input
          type="radio"
          checked={mode === "new"}
          onChange={() => {
            setMode("new");
            setValue("");
          }}
        />
        Create new term
      </label>
      {mode === "new" ? (
        <input
          required={!allowUnchangedEmpty}
          aria-label="New term name"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Fall 2026"
        />
      ) : null}
      {allowUnchangedEmpty && <small>Leave blank to keep the existing course term unchanged.</small>}
    </fieldset>
  );
}

function CredentialForm({
  profile,
  status,
  refresh,
}: {
  profile: AuthProfile;
  status?: CanvasStatus;
  refresh: () => void;
}) {
  const canvas = profile.auth_type === "canvas_token";
  const [mode, setMode] = useState<"pat" | "browser_session">(
    "pat",
  );
  const [advanced, setAdvanced] = useState(false);
  const [username, setUsername] = useState(profile.username ?? "");
  const [secret, setSecret] = useState("");
  const [base, setBase] = useState(
    profile.metadata_json.base_url ?? safeOrigin(profile.probe_url),
  );
  const [expiry, setExpiry] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    const endpoint = canvas
      ? mode === "pat"
        ? "canvas-token"
        : "canvas-browser-session"
      : "ntlm";
    const body = canvas
      ? mode === "pat"
        ? { base_url: base, token: secret, expiration_date: expiry || null }
        : { base_url: base, session_import: secret }
      : { username, password: secret };
    try {
      const result = await api<{ verified: boolean }>(
        `/api/auth/profiles/${profile.credential_id}/${endpoint}`,
        json("POST", body),
      );
      setMessage(
        result.verified ? "Verified and synchronized." : "Verification failed.",
      );
      setSecret("");
      refresh();
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "Verification failed",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="credential-form" onSubmit={submit}>
      {!canvas && <p role="status">Website authentication: {profile.state === "ACTIVE" ? "Verified" : profile.state.replaceAll("_", " ")}. This website uses its own NTLM credentials; Canvas / Gradescope browser login does not authenticate it.</p>}
      {canvas && <CanvasOAuthSignIn credentialId={profile.credential_id} />}
      {canvas && <CanvasCredentialSlots status={status} busy={busy} formatDate={formatUiucDateTime}
        onImport={method => { if (method === "pat") { setAdvanced(true); setMode("pat"); setSecret(""); } }}
        onRemove={async (method, generation) => {
          setBusy(true);
          try { await api(`/api/auth/profiles/${profile.credential_id}/canvas-method/${method}?generation=${generation}`, { method: "DELETE" }); refresh(); }
          catch (reason) { setMessage(String(reason)); }
          finally { setBusy(false); }
        }}
        onClear={async () => {
          if (!window.confirm("Clear all Canvas credentials? Cached course data will be preserved.")) return;
          setBusy(true);
          try { await api(`/api/auth/profiles/${profile.credential_id}/credential`, { method: "DELETE" }); refresh(); }
          catch (reason) { setMessage(String(reason)); }
          finally { setBusy(false); }
        }} />}
      <details open={!canvas || advanced} onToggle={event => { if (event.target === event.currentTarget) setAdvanced(event.currentTarget.open); }}>
      <summary>{canvas ? "Advanced: PAT / deprecated manual session import" : "Credentials"}</summary>
      {canvas ? (
        <div className="tabs compact">
          <button
            type="button"
            className={mode === "pat" ? "active" : ""}
            onClick={() => { setMode("pat"); setSecret(""); }}
          >
            PAT
          </button>
          <button
            type="button"
            className={mode === "browser_session" ? "active" : ""}
            onClick={() => { setMode("browser_session"); setSecret(""); }}
          >
            Deprecated manual session import
          </button>
        </div>
      ) : null}
      {canvas ? (
        <label>
          Canvas base URL
          <input
            type="url"
            value={base}
            onChange={(event) => setBase(event.target.value)}
          />
        </label>
      ) : (
        <label>
          Username
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
      )}
      {canvas && mode === "browser_session" && <details className="canvas-session-guide">
        <summary>如何获取 Browser Session（复制 cURL）</summary>
        <ol>
          <li>在 Chrome / Edge 中登录同一 Canvas 账号，打开 Canvas 页面。</li>
          <li>按 F12 → Network（网络），刷新页面，选择 Canvas 同域下成功的页面或 API 请求。</li>
          <li>右键请求 → Copy → Copy as cURL（优先 bash）；不要选择 Copy as PowerShell。</li>
          <li>把完整命令粘贴到下方，点击 Verify and sync。无需在终端执行命令。</li>
        </ol>
        <p>复制内容须包含 Cookie（-b 或 Cookie 请求头）。仅粘贴到本机此表单，不要发到聊天、截图或共享文件；导入器只解析 Cookie，不执行 cURL。</p>
        <a href="https://developer.chrome.com/docs/devtools/network/reference#copy" target="_blank" rel="noreferrer">Chrome DevTools 复制请求说明</a>
      </details>}
      <label>
        {canvas
          ? mode === "pat"
            ? "Canvas API token"
            : "Cookie header or copied cURL"
          : "Password"}
        {mode === "browser_session" ? (
          <textarea
            autoComplete="off"
            spellCheck={false}
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        ) : (
          <input
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        )}
      </label>
      {canvas && mode === "pat" ? (
        <label>
          Expiration date
          <input
            type="date"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
          />
        </label>
      ) : null}
      <button className="primary" disabled={!secret || busy}>
        {busy ? "Verifying…" : "Verify and sync"}
      </button>
      {message ? <p className="form-message">{message}</p> : null}
      </details>
    </form>
  );
}
function safeOrigin(value: string) {
  try {
    return new URL(value).origin;
  } catch {
    return "";
  }
}

function CalendarSettings({ courses }: { courses: Course[] }) {
  const client = useQueryClient();
  const icsAction = useAsyncAction();
  const calendarAction = useAsyncAction();
  const [draft, setDraft] = useState<EventDraft | undefined>();
  const [configOpen, setConfigOpen] = useState(false);
  const events = useQuery({
    queryKey: ["calendar-events", "agenda"], enabled: configOpen,
    queryFn: () => api<CalendarEvent[]>(`/api/calendar/events?${new URLSearchParams({
      start: DateTime.now().setZone("America/Chicago").startOf("day").toISO()!,
      end: DateTime.now().setZone("America/Chicago").startOf("day").plus({ days: 31 }).toISO()!,
    })}`),
  });
  const rules = useQuery({
    queryKey: ["availability"],
    queryFn: () => api<AvailabilityRule[]>("/api/calendar/availability"),
  });
  const overrides = useQuery({
    queryKey: ["availability-overrides"],
    queryFn: () =>
      api<AvailabilityOverride[]>("/api/calendar/availability/overrides"),
  });
  const imports = useQuery({
    queryKey: ["ics-imports"],
    queryFn: () => api<ICSImportSource[]>("/api/calendar/ics/imports"),
  });
  const [localRules, setLocalRules] = useState<AvailabilityRule[]>([]);
  const [eventEdit, setEventEdit] = useState<CalendarEvent | null | "new">(
    null,
  );
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<
    { file: File; preview: ICSPreview; timezone: string }[]
  >([]);
  const [override, setOverride] = useState({
    date: "",
    start: "",
    end: "",
    unavailable: false,
  });
  useEffect(() => {
    if (rules.data) setLocalRules(rules.data);
  }, [rules.data]);
  const rulesDirty =
    JSON.stringify(localRules.map(stripRule)) !==
    JSON.stringify((rules.data ?? []).map(stripRule));
  async function refresh() {
    await Promise.all(
      [
        ["calendar-events"],
        ["availability"],
        ["availability-overrides"],
        ["ics-imports"],
        ["today"],
      ].map((queryKey) => client.invalidateQueries({ queryKey })),
    );
  }
  async function previewFiles(next: File[]) {
    await icsAction.run("Preview calendars", async () => {
    setFiles(next);
    setPreviews([]);
      const values = [];
      for (const file of next) {
        const body = new FormData();
        body.append("file", file);
        values.push({
          file,
          preview: await api<ICSPreview>("/api/calendar/ics/preview", {
            method: "POST",
            body,
          }),
          timezone: "America/Chicago",
        });
      }
      setPreviews(values);
    });
  }
  async function importAll() {
    await icsAction.run("Import calendars", async () => {
      for (const row of previews) {
        const body = new FormData();
        body.append("file", row.file);
        if (row.preview.timezone_confirmation_required)
          body.append("timezone_override", row.timezone);
        body.append("confirm_removals", "false");
        await api("/api/calendar/ics/import", { method: "POST", body });
      }
      setFiles([]);
      setPreviews([]);
      await refresh();
    });
  }
  return (
    <>
      <div className="section-title">
        <div>
          <p className="eyebrow">CALENDAR</p>
          <h2>Availability and commitments</h2>
          <p>
            ICS imports stay read-only; Academic Watcher events remain editable.
          </p>
        </div>
        <button className="primary" onClick={() => {
          const start = DateTime.now().setZone("America/Chicago").startOf("hour");
          setDraft({ start_at: start.toISO()!, end_at: start.plus({ hours: 1 }).toISO()!, all_day: false });
          setEventEdit("new");
        }}>
          Add Event
        </button>
      </div>
      <VisualCalendar courses={courses} edit={setEventEdit} create={value => { setDraft(value); setEventEdit("new"); }} />
      <ActionFeedback action={calendarAction} />
      <details className="calendar-config" onToggle={event => setConfigOpen(event.currentTarget.open)}><summary>Imports, next 31 days agenda, availability and commute settings</summary>
      <GooglePanel />
      <section className="settings-panel">
        <h3>Import iCalendar files</h3>
        <p>
          Preview multiple .ics files before importing. Recurrence, exclusions,
          overrides, all-day dates, and source identity are retained.
        </p>
        <input
          aria-label="Choose iCalendar files"
          disabled={icsAction.busy}
          type="file"
          accept=".ics,text/calendar"
          multiple
          onChange={(event) =>
            void previewFiles([...(event.target.files ?? [])])
          }
        />
        {previews.length ? (
          <div className="ics-preview-list">
            {previews.map((row) => (
              <article key={`${row.file.name}-${row.file.size}`}>
                <strong>{row.preview.calendar_name ?? row.file.name}</strong>
                <p>
                  {row.preview.event_count} events ·{" "}
                  {row.preview.recurring_series_count} recurring series
                </p>
                <p>
                  New {row.preview.reconciliation.new} · Updated{" "}
                  {row.preview.reconciliation.updated} · Removed{" "}
                  {row.preview.reconciliation.removed} · Unchanged{" "}
                  {row.preview.reconciliation.unchanged}
                </p>
                {row.preview.timezone_confirmation_required ? (
                  <label>
                    Confirm timezone
                    <input
                      disabled={icsAction.busy}
                      value={row.timezone}
                      onChange={(event) =>
                        setPreviews((old) =>
                          old.map((item) =>
                            item === row
                              ? { ...item, timezone: event.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                  </label>
                ) : null}
              </article>
            ))}
          </div>
        ) : null}
        <button
          className="primary"
          disabled={
            icsAction.busy ||
            !files.length ||
            previews.length !== files.length ||
            previews.some(
              (row) =>
                row.preview.timezone_confirmation_required && !row.timezone,
            )
          }
          onClick={() => void importAll()}
        >
          {icsAction.busy ? "Working…" : "Import calendars"}
        </button>
        <ActionFeedback action={icsAction} />
        <div className="import-source-list">
          {imports.data?.map((row) => (
            <article key={row.id}>
              <div>
                <strong>{row.calendar_name ?? row.original_filename}</strong>
                <p>
                  {row.event_count} read-only events ·{" "}
                  {row.calendar_timezone ?? "event timezones"}
                </p>
              </div>
              <FilePickerButton disabled={icsAction.busy}
                label={`Re-import ${row.calendar_name ?? row.original_filename}`}
                onFile={file => { void icsAction.run("Re-import calendar", async () => {
                  const body = new FormData();
                  body.append("file", file);
                  body.append("source_id", String(row.id));
                  const preview = await api<ICSPreview>("/api/calendar/ics/preview", { method: "POST", body });
                  if (preview.reconciliation.removed &&
                      !confirm(`This re-import removes ${preview.reconciliation.removed} events. Continue?`)) return false;
                  body.append("confirm_removals", "true");
                  await api("/api/calendar/ics/import", { method: "POST", body });
                  await refresh();
                }); }}
              />
              <button
                className="ghost danger-text"
                disabled={icsAction.busy}
                onClick={() => { void icsAction.run("Remove calendar import", async () => {
                  if (
                    !confirm(
                      `Remove ${row.calendar_name ?? row.original_filename} and its imported events?`,
                    )
                  )
                    return false;
                  await api(`/api/calendar/ics/imports/${row.id}`, {
                    method: "DELETE",
                  });
                  await refresh();
                }); }}
              >
                Remove
              </button>
            </article>
          ))}
        </div>
      </section>
      <section className="settings-panel">
        <h3>Agenda</h3>
        <div className="agenda-list">
          {events.data?.map((item) => (
            <article key={item.id}>
              <div>
                <strong>{item.summary}</strong>
                <p>
                  {formatUiucDateTime(item.start_at)}–
                  {formatUiucDateTime(item.end_at)} · {item.source}
                  {item.read_only ? " · read-only" : ""}
                  {item.transparency === "transparent" ? " · free" : ""}
                </p>
              </div>
              {!item.read_only && item.source === "manual" ? (
                <div>
                  <button className="ghost" onClick={() => setEventEdit(item)}>
                    Edit
                  </button>
                  <button
                    className="ghost danger-text"
                    disabled={calendarAction.busy}
                    onClick={() => { void calendarAction.run("Delete event", async () => {
                      if (!confirm(item.recurrence_rule ? "Delete this entire recurring series?" : "Delete this event?")) return false;
                      await api(`/api/calendar/events/${item.id}`, {
                        method: "DELETE",
                      });
                      await refresh();
                    }); }}
                  >
                    Delete
                  </button>
                </div>
              ) : (
                <SourceStatusBadge state="READ ONLY" />
              )}
            </article>
          ))}
        </div>
      </section>
      <section className="settings-panel">
        <div className="section-title">
          <div>
            <h3>Weekly study availability</h3>
            <p>Multiple intervals per weekday are supported.</p>
          </div>
          <button
            className="secondary"
            onClick={() =>
              setLocalRules((old) => [
                ...old,
                {
                  weekday: 0,
                  start_local_time: "09:00",
                  end_local_time: "17:00",
                  enabled: true,
                },
              ])
            }
          >
            Add interval
          </button>
        </div>
        <div className="availability-list">
          {localRules.map((row, index) => (
            <div key={`${row.id ?? "new"}-${index}`}>
              <select
                value={row.weekday}
                onChange={(event) =>
                  setLocalRules((old) =>
                    old.map((item, i) =>
                      i === index
                        ? { ...item, weekday: Number(event.target.value) }
                        : item,
                    ),
                  )
                }
              >
                {WEEKDAYS.map((name, i) => (
                  <option key={name} value={i}>
                    {name}
                  </option>
                ))}
              </select>
              <input
                type="time"
                value={row.start_local_time}
                onChange={(event) =>
                  setLocalRules((old) =>
                    old.map((item, i) =>
                      i === index
                        ? { ...item, start_local_time: event.target.value }
                        : item,
                    ),
                  )
                }
              />
              <span>to</span>
              <input
                type="time"
                value={row.end_local_time}
                onChange={(event) =>
                  setLocalRules((old) =>
                    old.map((item, i) =>
                      i === index
                        ? { ...item, end_local_time: event.target.value }
                        : item,
                    ),
                  )
                }
              />
              <button
                className="ghost"
                onClick={() =>
                  setLocalRules((old) => old.filter((_, i) => i !== index))
                }
              >
                Remove
              </button>
            </div>
          ))}
        </div>
        <CommitButton
          label="Save availability"
          dirty={rulesDirty}
          onCommit={async () => {
            await api(
              "/api/calendar/availability",
              json("PUT", localRules.map(stripRule)),
            );
            await refresh();
          }}
        />
      </section>
      <section className="settings-panel">
        <h3>Date-specific overrides</h3>
        <div className="override-editor">
          <input
            type="date"
            value={override.date}
            onChange={(event) =>
              setOverride((old) => ({ ...old, date: event.target.value }))
            }
          />
          <label className="inline-check">
            <input
              type="checkbox"
              checked={override.unavailable}
              onChange={(event) =>
                setOverride((old) => ({
                  ...old,
                  unavailable: event.target.checked,
                }))
              }
            />
            Unavailable all day
          </label>
          {!override.unavailable ? (
            <>
              <input
                type="time"
                value={override.start}
                onChange={(event) =>
                  setOverride((old) => ({ ...old, start: event.target.value }))
                }
              />
              <input
                type="time"
                value={override.end}
                onChange={(event) =>
                  setOverride((old) => ({ ...old, end: event.target.value }))
                }
              />
            </>
          ) : null}
          <button
            className="secondary"
            disabled={
              calendarAction.busy ||
              !override.date ||
              (!override.unavailable && (!override.start || !override.end))
            }
            onClick={() => { void calendarAction.run("Save override", async () => {
              await api(
                `/api/calendar/availability/overrides/${override.date}`,
                json("PUT", {
                  date: override.date,
                  available_intervals: override.unavailable
                    ? []
                    : [{ start: override.start, end: override.end }],
                }),
              );
              setOverride({ date: "", start: "", end: "", unavailable: false });
              await refresh();
            }); }}
          >
            Save override
          </button>
        </div>
        <div className="override-list">
          {overrides.data?.map((row) => (
            <span key={row.date}>
              {row.date}:{" "}
              {row.available_intervals.length
                ? row.available_intervals
                    .map((value) => `${value.start}–${value.end}`)
                    .join(", ")
                : "Unavailable"}
              <button
                className="ghost"
                aria-label={`Remove override for ${row.date}`}
                disabled={calendarAction.busy}
                onClick={() => { void calendarAction.run("Remove override", async () => {
                  await api(
                    `/api/calendar/availability/overrides/${row.date}`,
                    { method: "DELETE" },
                  );
                  await refresh();
                }); }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      </section>
      <section className="settings-panel">
        <h3>Course commute buffers</h3>
        <div className="commute-grid">
          {courses.map((course) => (
            <CommuteEditor key={course.id} course={course} refresh={refresh} />
          ))}
        </div>
      </section>
      </details>
      {eventEdit ? (
        <EventEditor
          value={eventEdit === "new" ? null : eventEdit}
          draft={draft}
          courses={courses}
          close={() => setEventEdit(null)}
          saved={async () => {
            setEventEdit(null);
            await refresh();
          }}
        />
      ) : null}
    </>
  );
}
function stripRule(row: AvailabilityRule) {
  return {
    weekday: row.weekday,
    start_local_time: row.start_local_time,
    end_local_time: row.end_local_time,
    enabled: row.enabled,
  };
}
function GooglePanel() {
  return (
    <section className="settings-panel google-calendar-panel">
      <div className="section-title">
        <div>
          <h3>Google Calendar</h3>
          <p>
            Direct Google Calendar connection is planned for a later release.
          </p>
        </div>
        <SourceStatusBadge state="COMING SOON" />
      </div>
      <p className="form-message">
        Coming Soon — use iCalendar import below.
      </p>
    </section>
  );
}

function CommuteEditor({
  course,
  refresh,
}: {
  course: Course;
  refresh: () => void;
}) {
  const [value, setValue] = useState(course.commute_minutes ?? 0);
  return (
    <label>
      {course.display_course_code ?? course.course_code}
      <span>
        <input
          type="number"
          min="0"
          max="240"
          step="5"
          value={value}
          onChange={(event) => setValue(Number(event.target.value))}
        />
        <CommitButton
          label="Save"
          dirty={value !== course.commute_minutes}
          onCommit={async () => {
            await api(
              `/api/courses/${course.id}/commute`,
              json("PATCH", { commute_minutes: value }),
            );
            refresh();
          }}
        />
      </span>
    </label>
  );
}

function EventEditor({
  value,
  draft,
  courses,
  close,
  saved,
}: {
  value: CalendarEvent | null;
  draft?: EventDraft;
  courses: Course[];
  close: () => void;
  saved: () => void;
}) {
  const zone = value?.timezone ?? "America/Chicago";
  const invalidStoredZone = !DateTime.fromMillis(0, { zone }).isValid;
  const displayZone = invalidStoredZone ? "America/Chicago" : zone;
  const local = (raw: string | undefined) =>
    raw ? DateTime.fromISO(raw, { zone: "utc" }).setZone(displayZone).toFormat("yyyy-MM-dd'T'HH:mm") : "";
  const initial = {
    summary: value?.summary ?? "",
    description: value?.description ?? "",
    location: value?.location ?? "",
    start: local(value?.start_at ?? draft?.start_at),
    end: local(value?.end_at ?? draft?.end_at),
    timezone: value?.timezone ?? "America/Chicago",
    all_day: value?.all_day ?? draft?.all_day ?? false,
    event_type: value?.event_type ?? "other",
    course_id: value?.course_id ? String(value.course_id) : "",
    recurrence_rule: value?.recurrence_rule ?? "",
  };
  const [form, setForm] = useState(initial);
  const [error, setError] = useState("");
  const set = (key: string, next: string | boolean) =>
    setForm((old) => ({ ...old, [key]: next }));
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <section className="dialog event-editor" role="dialog" aria-modal="true" aria-label="Event editor" onKeyDown={event => event.key === "Escape" && !event.nativeEvent.isComposing && close()}>
        {invalidStoredZone && <p role="alert" className="form-message error">Stored timezone is invalid. Existing times are shown in America/Chicago; correct the timezone before saving.</p>}
        <div className="dialog-head">
          <h2>{value ? "Edit event" : "Add event"}</h2>
          <button className="icon-button" aria-label="Close event editor" onClick={close}>
            ×
          </button>
        </div>
        <div className="form-grid">
          <label>
            Summary
            <input
              autoFocus
              value={form.summary}
              onChange={(event) => set("summary", event.target.value)}
            />
          </label>
          <label>
            Description
            <textarea
              value={form.description}
              onChange={(event) => set("description", event.target.value)}
            />
          </label>
          <label>
            Location
            <input
              value={form.location}
              onChange={(event) => set("location", event.target.value)}
            />
          </label>
          <div className="two-columns">
            <label>
              Start
              <input
                type="datetime-local"
                value={form.start}
                onChange={(event) => set("start", event.target.value)}
              />
            </label>
            <label>
              End (exclusive for all-day events)
              <input
                type="datetime-local"
                value={form.end}
                onChange={(event) => set("end", event.target.value)}
              />
            </label>
          </div>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={form.all_day}
              onChange={(event) => set("all_day", event.target.checked)}
            />
            All-day event
          </label>
          <label>
            Timezone
            <input
              value={form.timezone}
              onChange={(event) => set("timezone", event.target.value)}
            />
          </label>
          <label>
            Type
            <select
              value={form.event_type}
              onChange={(event) => set("event_type", event.target.value)}
            >
              <option value="class">Class</option>
              <option value="busy">Busy</option>
              <option value="personal">Personal</option>
              <option value="study">Study</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label>
            Course
            <select
              value={form.course_id}
              onChange={(event) => set("course_id", event.target.value)}
            >
              <option value="">No course</option>
              {courses.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.display_course_code ?? row.course_code}
                </option>
              ))}
            </select>
          </label>
          <label>
            RRULE (optional)
            <input
              value={form.recurrence_rule}
              placeholder="FREQ=WEEKLY;BYDAY=MO,WE"
              onChange={(event) => set("recurrence_rule", event.target.value)}
            />
          </label>
          <CommitButton
            label="Save event"
            dirty={JSON.stringify(form) !== JSON.stringify(initial)}
            disabled={!form.summary || !form.start || !form.end}
            onCommit={async () => {
              try {
                if (!DateTime.fromMillis(0, { zone: form.timezone }).isValid)
                  throw new Error("Unknown timezone; use an IANA timezone such as America/Chicago");
                const parse = (raw: string) => {
                  const normalized = form.all_day ? `${raw.slice(0, 10)}T00:00` : raw;
                  const date = DateTime.fromISO(normalized, { zone: form.timezone });
                  if (!date.isValid || date.toFormat("yyyy-MM-dd'T'HH:mm") !== normalized)
                    throw new Error("Invalid local time or a daylight-saving time gap. Choose another time.");
                  if (date.getPossibleOffsets().length > 1)
                    throw new Error("This time occurs twice at the daylight-saving transition. Choose an unambiguous time.");
                  return date.toUTC().toISO()!;
                };
                await api(
                  value
                    ? `/api/calendar/events/${value.id}`
                    : "/api/calendar/events",
                  json(value ? "PUT" : "POST", {
                    summary: form.summary,
                    description: form.description,
                    location: form.location || null,
                    start_at: parse(form.start),
                    end_at: parse(form.end),
                    all_day: form.all_day,
                    timezone: form.timezone,
                    recurrence_rule: form.recurrence_rule || null,
                    event_type: form.event_type,
                    course_id: form.course_id ? Number(form.course_id) : null,
                  }),
                );
                saved();
              } catch (reason) {
                setError(
                  reason instanceof Error
                    ? reason.message
                    : "Unable to save event",
                );
                throw reason;
              }
            }}
          />
          {error ? <p role="alert" className="form-message error">{error}</p> : null}
        </div>
      </section>
    </div>
  );
}
