import { SourceStatusBadge } from "./SourceStatusBadge";
import type { CanvasStatus } from "./types";

type Method = "oauth" | "pat" | "browser_session";

export function CanvasCredentialSlots({ status, busy, formatDate, onImport, onRemove, onClear }: {
  status?: CanvasStatus;
  busy: boolean;
  formatDate: (value: string) => string;
  onImport: (method: Method) => void;
  onRemove: (method: Method, generation: number) => void;
  onClear: () => void;
}) {
  const present = Object.values(status?.credentials ?? {}).some(slot => slot.present);
  return <section className="canvas-slots" aria-label="Canvas credentials">
    <div className="canvas-connection-summary">
      <span>Connection: <strong>{status?.connection_state ?? status?.credential_state ?? "Unknown"}</strong>
        {status?.effective_method ? ` via ${status.effective_method === "oauth" ? "OAuth" : status.effective_method === "pat" ? "PAT" : "Browser Session"}` : ""}</span>
      <span>Automatic backup: <strong>{status?.backup_ready ? "Ready" : "Unavailable"}</strong></span>
    </div>
    <div className="canvas-credential-list">
      {(["oauth", "pat", "browser_session"] as const).map(method => {
        const slot = status?.credentials?.[method];
        const name = method === "oauth" ? "OAuth" : method === "pat" ? "PAT" : "Browser Session";
        const detail = method === "pat"
          ? `Expires: ${slot?.expires_at ? `${formatDate(slot.expires_at)} (${slot.expiration_source ?? "Unknown source"})` : "Unknown"}`
          : `Verified: ${slot?.last_verified_at ? formatDate(slot.last_verified_at) : "Not verified"}`;
        return <article className="canvas-credential-row" key={method} aria-label={`${name} credential`}>
          <div className="canvas-credential-heading">
            <strong>{name} <span className="canvas-credential-role">· {method === status?.effective_method ? "Current" : "Backup"}</span></strong>
            <SourceStatusBadge state={slot?.state ?? "MISSING"} />
          </div>
          <div className="canvas-credential-details">
            <small>{detail}{method === "pat" && slot?.origin === "environment" ? " · Environment" : ""}</small>
            <div className="canvas-slot-actions">
              {method === "pat" && <button className="ghost canvas-credential-action" type="button" disabled={busy}
                aria-label={`Import or replace ${name}`} onClick={() => onImport(method)}>Import / Replace</button>}
              <button className="ghost canvas-credential-action canvas-credential-remove" type="button" disabled={busy || !slot?.present}
                aria-label={`Remove ${name}`} onClick={() => onRemove(method, slot?.generation ?? 0)}>Remove</button>
            </div>
          </div>
          {method === "pat" && slot?.origin === "environment" && <small className="canvas-environment-note">
            Removing disables this PAT until restart; .env is unchanged.
          </small>}
        </article>;
      })}
    </div>
    <div className="canvas-slot-footer">
      <small>Default priority: OAuth → PAT → Browser Session. Credentials are independently verified.</small>
      <button className="ghost canvas-credential-action canvas-credential-remove" type="button"
        disabled={busy || !present} onClick={onClear}>Clear all credentials</button>
    </div>
  </section>;
}
