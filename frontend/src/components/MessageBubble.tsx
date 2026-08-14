import ReactMarkdown from "react-markdown";
import type { Message } from "../api/types";

/**
 * A conversational (non-diagnosis) agent reply — the lightweight counterpart to
 * `DiagnosisCard`. felix returns a `Message` for follow-ups where a rigid
 * root-cause/steps card would be overkill (e.g. "how do I rerun the service?").
 * The body is GitHub-flavored Markdown, so it's rendered with react-markdown;
 * the footer mirrors DiagnosisCard's (citations + show-evidence) for continuity.
 */
export function MessageBubble({
  message,
  active,
  onShowEvidence,
}: {
  message: Message;
  active: boolean;
  onShowEvidence: () => void;
}) {
  const citations = message.cited_incident_ids.length + message.cited_change_ids.length;

  return (
    <div className="message">
      <div className="message__body markdown">
        <ReactMarkdown>{message.text}</ReactMarkdown>
      </div>

      <div className="diagnosis__footer">
        {citations > 0 && (
          <span className="badge badge--cite" title="Memory records cited by this reply">
            {citations} citation{citations === 1 ? "" : "s"}
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
