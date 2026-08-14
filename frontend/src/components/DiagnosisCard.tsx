import type { Diagnosis, EvidencePacket, ProposedStep } from "../api/types";
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
    </div>
  );
}
