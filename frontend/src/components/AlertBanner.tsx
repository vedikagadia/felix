import { useEffect, useState } from "react";
import { subscribeToAlerts } from "../api/alerts";
import type { AlertPayload } from "../api/types";

// styles.css is owned by another team, so the banner is styled inline off the
// shared CSS variables (see :root in styles.css) to stay on-theme without
// editing that file.
const S = {
  region: {
    borderBottom: "1px solid var(--border)",
    background: "var(--bg-elev)",
    padding: "10px 16px",
  },
  head: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    fontSize: 12,
    textTransform: "uppercase" as const,
    letterSpacing: "0.6px",
    color: "var(--inc)",
    marginBottom: 8,
  },
  pulse: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    background: "var(--inc)",
    flexShrink: 0,
  },
  list: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column" as const,
    gap: 6,
  },
  button: {
    width: "100%",
    textAlign: "left" as const,
    background: "var(--bg)",
    border: "1px solid var(--border)",
    borderLeft: "2px solid var(--inc)",
    borderRadius: "var(--radius)",
    padding: "8px 12px",
    cursor: "pointer",
    color: "var(--text)",
  },
  buttonActive: { borderColor: "var(--accent)", borderLeftColor: "var(--accent)" },
  top: { display: "flex", alignItems: "center", gap: 6, marginBottom: 4 },
  time: { marginLeft: "auto", fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--mono)" },
  summary: { margin: 0, fontSize: 13, color: "var(--text-dim)" },
} satisfies Record<string, React.CSSProperties>;

/**
 * Live banner of open CDC alerts. Subscribes to the alert seam on mount (which
 * polls every ~3s and dedupes on the backend), and surfaces each raised alert.
 * Clicking one hands it up to App to open its triage session in the chat.
 */
export function AlertBanner({
  onSelect,
  activeSessionId,
}: {
  onSelect: (alert: AlertPayload) => void;
  /** The session currently open in the chat, so it can be marked as viewing. */
  activeSessionId: string | null;
}) {
  const [alerts, setAlerts] = useState<AlertPayload[]>([]);

  useEffect(() => subscribeToAlerts(setAlerts), []);

  if (alerts.length === 0) return null;

  return (
    <div style={S.region} role="region" aria-label="Live alerts">
      <div style={S.head}>
        <span style={S.pulse} />
        {alerts.length} live alert{alerts.length === 1 ? "" : "s"}
      </div>
      <ul style={S.list}>
        {alerts.map((alert) => (
          <li key={alert.session_id}>
            <button
              type="button"
              style={
                alert.session_id === activeSessionId
                  ? { ...S.button, ...S.buttonActive }
                  : S.button
              }
              onClick={() => onSelect(alert)}
            >
              <div style={S.top}>
                {alert.service && <span className="tag tag--muted">{alert.service}</span>}
                {alert.metric && <span className="tag tag--muted">{alert.metric}</span>}
                <span style={S.time}>{relativeTime(alert.created_at)}</span>
              </div>
              <p style={S.summary}>{alert.summary}</p>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** "just now" / "3m ago" — a compact age for a live feed; falls back to the raw string. */
function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
