import { useEffect, useRef, useState } from "react";
import type { Turn } from "../App";
import type { EvidencePacket, NodeHealth } from "../api/types";

/**
 * Reasoning-replay overlay — a brief center-stage animation of the memory felix
 * walked for an alert, played BEFORE the diagnosis is revealed underneath.
 *
 * It visualises, stage by stage, the same evidence the panel then shows: what it
 * recalled by meaning (incidents / docs / changes / runbooks — vector search),
 * the code-graph trace it followed, and the LIVE downstream metrics it queried
 * (the topology-health sweep — breached deps glow red with their observed value
 * vs. threshold). The stages advance on timers; the overlay only dismisses once
 * BOTH the animation has played through and the model's response has landed, so
 * a fast diagnosis still gets the full replay and a slow one holds on the last
 * stage rather than popping the diagnosis mid-generation.
 *
 * Purely presentational: it reads the streaming Turn (evidence arrives before the
 * response) and calls `onDone` when it's finished; App clears it from view then.
 */

const STEP_MS = 1400; // dwell per stage
const FADE_MS = 420; // exit fade

interface Stage {
  key: string;
  label: string;
  dot: string;
  /** Is there anything for this stage yet? (drives the count / spinner) */
  ready: (p: EvidencePacket) => boolean;
}

const STAGES: Stage[] = [
  {
    key: "recall",
    label: "Recalling memory by meaning",
    dot: "inc",
    ready: (p) =>
      p.incidents.length + p.docs.length + p.changes.length + (p.runbooks?.length ?? 0) > 0,
  },
  {
    key: "graph",
    label: "Tracing the code graph upstream",
    dot: "graph",
    ready: (p) => p.upstream.length > 0,
  },
  {
    key: "topology",
    label: "Querying live downstream health",
    dot: "metric",
    ready: (p) => (p.topology_health?.length ?? 0) > 0,
  },
  {
    key: "highlight",
    label: "Highlighting what mattered",
    dot: "cite",
    ready: () => true,
  },
];

export function ReasoningOverlay({ turn, onDone }: { turn: Turn; onDone: () => void }) {
  // How far the animation has walked (0..STAGES.length). Advances on a timer.
  const [active, setActive] = useState(0);
  const [leaving, setLeaving] = useState(false);
  const evidence = turn.evidence ?? turn.response?.evidence ?? null;
  // The response is "settled" once a diagnosis/message landed or the turn errored.
  const settled = turn.response != null || turn.error != null;

  // `onDone` is a fresh closure each App render; keep it in a ref so the dismiss
  // effect doesn't list it as a dep (which would re-run the effect, clear the
  // pending dismiss timeout, and — since `leaving` is now true — never re-arm it,
  // leaving the overlay mounted at opacity 0 and eating every click/scroll).
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  // Advance one stage per tick. Don't step past `recall` until evidence has
  // actually streamed in (so the first stage shows a spinner, not "0 recalled").
  useEffect(() => {
    if (active >= STAGES.length) return;
    if (active === 0 && !evidence) return; // wait for the recall result
    const t = setTimeout(() => setActive((s) => s + 1), STEP_MS);
    return () => clearTimeout(t);
  }, [active, evidence]);

  // Dismiss once the walk is complete AND the model has settled — reveal the
  // diagnosis rendering underneath. A short fade, then hand back to App.
  useEffect(() => {
    if (active < STAGES.length || !settled || leaving) return;
    setLeaving(true);
    const t = setTimeout(() => onDoneRef.current(), FADE_MS);
    return () => clearTimeout(t);
  }, [active, settled, leaving]);

  // Let the operator skip the replay (Esc or the Skip button).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") skip();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const skipped = useRef(false);
  function skip() {
    if (skipped.current) return;
    skipped.current = true;
    setLeaving(true);
    setTimeout(() => onDoneRef.current(), FADE_MS);
  }

  const cited = new Set([
    ...(turn.response?.diagnosis?.cited_incident_ids ?? []),
    ...(turn.response?.diagnosis?.cited_change_ids ?? []),
    ...(turn.response?.message?.cited_incident_ids ?? []),
    ...(turn.response?.message?.cited_change_ids ?? []),
  ]);

  return (
    <div className={`replay ${leaving ? "replay--leaving" : ""}`} role="dialog" aria-label="felix is reasoning">
      <div className="replay__card">
        <header className="replay__head">
          <span className="replay__logo">🦊</span>
          <div>
            <h3>felix is reasoning…</h3>
            <p className="replay__alert">{turn.request.alert}</p>
          </div>
          <button type="button" className="replay__skip" onClick={skip}>
            Skip ↵
          </button>
        </header>

        <ol className="replay__stages">
          {STAGES.map((stage, i) => (
            <StageRow
              key={stage.key}
              stage={stage}
              state={i < active ? "done" : i === active ? "active" : "pending"}
              evidence={evidence}
              cited={cited}
            />
          ))}
        </ol>

        <footer className="replay__foot">
          {settled ? "diagnosis ready" : "reasoning over the recalled evidence…"}
        </footer>
      </div>
    </div>
  );
}

function StageRow({
  stage,
  state,
  evidence,
  cited,
}: {
  stage: Stage;
  state: "pending" | "active" | "done";
  evidence: EvidencePacket | null;
  cited: Set<string>;
}) {
  const shown = state !== "pending";
  return (
    <li className={`replay__stage replay__stage--${state}`}>
      <span className="replay__marker">
        <span className={`dot dot--${stage.dot}`} />
      </span>
      <div className="replay__stagebody">
        <span className="replay__label">{stage.label}</span>
        {shown && evidence && <StageDetail stageKey={stage.key} evidence={evidence} cited={cited} />}
        {state === "active" && !evidence && <span className="replay__spinner">querying…</span>}
      </div>
    </li>
  );
}

/** The per-stage payload preview — the actual recalled/queried items. */
function StageDetail({
  stageKey,
  evidence,
  cited,
}: {
  stageKey: string;
  evidence: EvidencePacket;
  cited: Set<string>;
}) {
  if (stageKey === "recall") {
    const chips: Array<[string, number]> = [
      ["incidents", evidence.incidents.length],
      ["docs", evidence.docs.length],
      ["changes", evidence.changes.length],
      ["runbooks", evidence.runbooks?.length ?? 0],
    ];
    return (
      <span className="replay__chips">
        {chips
          .filter(([, n]) => n > 0)
          .map(([label, n]) => (
            <span key={label} className="replay__chip">
              {n} {label}
            </span>
          ))}
        {chips.every(([, n]) => n === 0) && <span className="replay__chip replay__chip--none">none</span>}
      </span>
    );
  }

  if (stageKey === "graph") {
    if (evidence.upstream.length === 0)
      return <span className="replay__chip replay__chip--none">no code-graph origin</span>;
    return (
      <span className="replay__trace">
        {evidence.upstream.map((h, i) => (
          <span key={h.node.id} className="replay__tracenode">
            {i > 0 && <span className="replay__arrow">→</span>}
            <code>{h.node.name}</code>
          </span>
        ))}
      </span>
    );
  }

  if (stageKey === "topology") {
    const breached = evidence.topology_health ?? [];
    if (breached.length === 0)
      return <span className="replay__chip replay__chip--none">downstream healthy</span>;
    return (
      <span className="replay__chips">
        {breached.map((nh) => (
          <span key={`${nh.service}::${nh.metric}`} className="replay__chip replay__chip--breach">
            ⚠ {nh.service} {fmtReading(nh)}
          </span>
        ))}
      </span>
    );
  }

  // highlight: what the diagnosis actually cited (empty until the response lands)
  if (cited.size === 0)
    return <span className="replay__chip replay__chip--none">weighing the evidence…</span>;
  return (
    <span className="replay__chips">
      {[...cited].map((id) => (
        <span key={id} className="replay__chip replay__chip--cite">
          cited {id}
        </span>
      ))}
    </span>
  );
}

function fmtReading(nh: NodeHealth): string {
  const f = (v: number) =>
    nh.intent === "error_rate"
      ? `${(v * 100).toFixed(0)}%`
      : nh.metric.endsWith("_ms")
        ? `${v.toFixed(0)}ms`
        : v.toFixed(0);
  return `${nh.intent} ${f(nh.observed)} ≥ ${f(nh.threshold)}`;
}
