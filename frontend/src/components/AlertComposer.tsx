import { useState } from "react";
import type { ChatRequest } from "../api/types";

const EXAMPLES = [
  "checkout failing, db.pool.exhausted during spike",
  "customers report slow checkout but dashboards look fine",
];

export function AlertComposer({
  onSubmit,
  disabled,
  continuing = false,
  onNewIncident,
}: {
  onSubmit: (req: ChatRequest) => void;
  disabled: boolean;
  /** True when a conversation is open — the next message is a follow-up. */
  continuing?: boolean;
  /** Clear the conversation so the next message opens a fresh incident. */
  onNewIncident?: () => void;
}) {
  const [alert, setAlert] = useState("");
  const [originNode, setOriginNode] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);

  function fire() {
    const trimmed = alert.trim();
    if (!trimmed || disabled) return;
    onSubmit({
      alert: trimmed,
      origin_node: originNode.trim() || undefined,
    });
    setAlert("");
    // keep origin_node — an operator often triages several alerts on one node
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter submits; Shift+Enter for a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      fire();
    }
  }

  return (
    <div className="composer">
      {continuing ? (
        <div className="composer__session">
          <span className="composer__session-note">
            Following up on the current incident — felix remembers the conversation.
          </span>
          <button
            type="button"
            className="composer__new"
            disabled={disabled}
            onClick={onNewIncident}
          >
            + New incident
          </button>
        </div>
      ) : (
        alert.trim() === "" && (
          <div className="composer__examples">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                className="chip"
                disabled={disabled}
                onClick={() => setAlert(ex)}
              >
                {ex}
              </button>
            ))}
          </div>
        )
      )}

      <div className="composer__row">
        <textarea
          className="composer__input"
          placeholder={
            continuing
              ? "Ask a follow-up… e.g. “did scaling the DB help?” (Enter to send)"
              : "Paste an alert or describe the symptom… (Enter to send, Shift+Enter for newline)"
          }
          value={alert}
          rows={2}
          disabled={disabled}
          onChange={(e) => setAlert(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button className="btn btn--send" onClick={fire} disabled={disabled || !alert.trim()}>
          {disabled ? "…" : continuing ? "Follow up" : "Diagnose"}
        </button>
      </div>

      <div className="composer__advanced">
        <button
          type="button"
          className="composer__toggle"
          onClick={() => setShowAdvanced((v) => !v)}
        >
          {showAdvanced ? "▾" : "▸"} Advanced
        </button>
        {showAdvanced && (
          <label className="composer__field">
            <span>Origin node</span>
            <input
              type="text"
              placeholder="e.g. ConnectionPool.acquire — pins the code-graph trace"
              value={originNode}
              disabled={disabled}
              onChange={(e) => setOriginNode(e.target.value)}
            />
          </label>
        )}
      </div>
    </div>
  );
}
