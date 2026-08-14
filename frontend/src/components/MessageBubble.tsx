import ReactMarkdown from "react-markdown";
import type { EvidencePacket, Message } from "../api/types";
import { Citations } from "./Citations";

/**
 * A conversational (non-diagnosis) agent reply — the lightweight counterpart to
 * `DiagnosisCard`. felix returns a `Message` for follow-ups where a rigid
 * root-cause/steps card would be overkill (e.g. "how do I rerun the service?").
 * The body is GitHub-flavored Markdown, so it's rendered with react-markdown;
 * the footer mirrors DiagnosisCard's (citations + show-evidence) for continuity.
 */
export function MessageBubble({
  message,
  evidence,
  active,
  onShowEvidence,
  activeCitation,
  onCitationFocus,
}: {
  message: Message;
  evidence?: EvidencePacket;
  active: boolean;
  onShowEvidence: () => void;
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
}) {
  return (
    <div className="message">
      <div className="message__body markdown">
        <ReactMarkdown>{message.text}</ReactMarkdown>
      </div>

      <Citations
        incidentIds={message.cited_incident_ids}
        changeIds={message.cited_change_ids}
        evidence={evidence}
        activeCitation={activeCitation}
        onCitationFocus={onCitationFocus}
        onShowEvidence={onShowEvidence}
      />

      <div className="diagnosis__footer">
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
