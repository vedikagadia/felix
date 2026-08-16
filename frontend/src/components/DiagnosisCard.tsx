import { useState } from "react";
import type { Diagnosis, EvidencePacket, ProposedStep } from "../api/types";
import { submitFeedback } from "../api/client";
import { Citations } from "./Citations";

function isStepObject(s: ProposedStep | string): s is ProposedStep {
  return typeof s === "object" && s !== null;
}

export function DiagnosisCard({
  diagnosis,
  evidence,
  active,
  onShowEvidence,
  activeCitation,
  onCitationFocus,
}: {
  diagnosis: Diagnosis;
  evidence?: EvidencePacket;
  active: boolean;
  onShowEvidence: () => void;
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
}) {
  const conf = diagnosis.confidence;

  return (
    <div className="diagnosis">
      <p className="diagnosis__summary">{diagnosis.summary}</p>

      {diagnosis.root_cause && (
        <div className="diagnosis__section">
          <h4>Root cause</h4>
          <p>{diagnosis.root_cause}</p>
        </div>
      )}

      {diagnosis.proposed_steps.length > 0 && (
        <div className="diagnosis__section">
          <h4>Proposed steps</h4>
          <ol className="steps">
            {diagnosis.proposed_steps.map((step, i) => (
              <li key={i} className="steps__item">
                {isStepObject(step) ? (
                  <>
                    <span className="steps__action">{step.action}</span>
                    {step.command && <code className="steps__command">{step.command}</code>}
                    {step.outcome && <span className="steps__outcome">→ {step.outcome}</span>}
                  </>
                ) : (
                  <span className="steps__action">{step}</span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      <Citations
        incidentIds={diagnosis.cited_incident_ids}
        changeIds={diagnosis.cited_change_ids}
        evidence={evidence}
        activeCitation={activeCitation}
        onCitationFocus={onCitationFocus}
        onShowEvidence={onShowEvidence}
      />

      <div className="diagnosis__footer">
        {conf != null && (
          <span
            className={`badge ${conf >= 0.7 ? "badge--high" : conf >= 0.4 ? "badge--mid" : "badge--low"}`}
            title="Model-reported confidence"
          >
            confidence {(conf * 100).toFixed(0)}%
          </span>
        )}
        <button
          className={`btn btn--ghost ${active ? "is-active" : ""}`}
          onClick={onShowEvidence}
        >
          {active ? "Showing evidence →" : "Show evidence"}
        </button>
      </div>

      {diagnosis.incident_id && <FeedbackBar incidentId={diagnosis.incident_id} />}
    </div>
  );
}

/**
 * 👍/👎 on a diagnosis — felix's learning loop. 👍 promotes this incident into
 * recallable memory (the backend embeds it), so future similar alerts recall it;
 * 👎 keeps it out. Optimistic: locks after one vote, shows what the vote did.
 */
function FeedbackBar({ incidentId }: { incidentId: string }) {
  const [state, setState] = useState<"idle" | "sending" | "helpful" | "not_helpful" | "error">(
    "idle",
  );

  async function vote(helpful: boolean) {
    if (state === "sending" || state === "helpful" || state === "not_helpful") return;
    setState("sending");
    try {
      const res = await submitFeedback(incidentId, helpful);
      setState(res.feedback);
    } catch {
      setState("error");
    }
  }

  if (state === "helpful") {
    return (
      <div className="feedback feedback--done">
        ✓ Saved to memory — felix will recall this next time a similar alert fires.
      </div>
    );
  }
  if (state === "not_helpful") {
    return <div className="feedback feedback--done">Thanks — kept out of recall.</div>;
  }

  return (
    <div className="feedback">
      <span className="feedback__prompt">Was this helpful?</span>
      <button
        type="button"
        className="feedback__btn"
        disabled={state === "sending"}
        onClick={() => vote(true)}
        title="Store this problem + solution so felix recalls it next time"
      >
        👍 Helpful
      </button>
      <button
        type="button"
        className="feedback__btn"
        disabled={state === "sending"}
        onClick={() => vote(false)}
        title="Don't store this — keep it out of recall"
      >
        👎 Not helpful
      </button>
      {state === "error" && <span className="feedback__error">Couldn’t save — try again.</span>}
    </div>
  );
}
