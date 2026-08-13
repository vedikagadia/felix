import { useEffect, useRef } from "react";
import type { Turn } from "../App";
import { DiagnosisCard } from "./DiagnosisCard";

export function ChatThread({
  turns,
  activeId,
  onSelect,
}: {
  turns: Turn[];
  activeId: number | null;
  onSelect: (id: number) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
    // Depend on the streaming text too, so the view follows tokens as they land.
  }, [turns]);

  if (turns.length === 0) {
    return (
      <div className="thread thread--empty">
        <div className="empty">
          <div className="empty__logo">🦊</div>
          <h2>What's on fire?</h2>
          <p>
            Paste an alert or describe a symptom. felix recalls similar past incidents, relevant
            docs, and recent merges, traces the code graph, and proposes a diagnosis.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="thread">
      {turns.map((turn) => (
        <div key={turn.id} className="turn">
          <div className="msg msg--user">
            <div className="msg__role">alert</div>
            <div className="msg__bubble">
              <p className="msg__text">{turn.request.alert}</p>
              {turn.request.origin_node && (
                <p className="msg__meta">origin node: {turn.request.origin_node}</p>
              )}
            </div>
          </div>

          <div className="msg msg--agent">
            <div className="msg__role">felix</div>
            <div className="msg__bubble">
              {turn.pending && !turn.streamingText && <ThinkingDots />}
              {turn.pending && turn.streamingText && (
                <StreamingReasoning text={turn.streamingText} />
              )}
              {turn.error && <div className="error">{turn.error}</div>}
              {turn.response && (
                <DiagnosisCard
                  diagnosis={turn.response.diagnosis}
                  active={turn.id === activeId}
                  onShowEvidence={() => onSelect(turn.id)}
                />
              )}
            </div>
          </div>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

function ThinkingDots() {
  return (
    <div className="thinking">
      recalling memory<span className="thinking__dots" />
    </div>
  );
}

/**
 * The model's output as it streams in, before the parsed DiagnosisCard swaps in.
 * The backend streams a raw JSON diagnosis, so this shows felix "drafting" —
 * honest and lively for a demo. A blinking caret marks the live edge.
 */
function StreamingReasoning({ text }: { text: string }) {
  return (
    <div className="streaming">
      <div className="streaming__label">drafting diagnosis…</div>
      <pre className="streaming__text">
        {text}
        <span className="streaming__caret" />
      </pre>
    </div>
  );
}
