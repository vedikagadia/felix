import { useEffect, useRef, useState } from "react";
import type { Turn } from "../App";
import type {
  CodeChange,
  DocChunk,
  EvidencePacket,
  GraphHit,
  Incident,
  NodeHealth,
  Recall,
  Runbook,
} from "../api/types";
import { relevancePct } from "../lib/relevance";

export function EvidencePanel({
  turn,
  activeCitation,
  onCitationFocus,
}: {
  turn: Turn | null;
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
}) {
  if (!turn) {
    return (
      <div className="evidence__empty">
        <h3>Evidence</h3>
        <p>Send an alert, then pick a reply to inspect the memory felix recalled for it.</p>
        <ul className="evidence__legend">
          <li>
            <span className="dot dot--inc" /> similar past incidents
          </li>
          <li>
            <span className="dot dot--doc" /> relevant docs
          </li>
          <li>
            <span className="dot dot--chg" /> recent code changes
          </li>
          <li>
            <span className="dot dot--graph" /> upstream call trace
          </li>
        </ul>
      </div>
    );
  }

  // Evidence arrives (streamed) before the final response, so prefer whichever
  // we have — this lets the panel fill while the diagnosis is still generating.
  const evidence = turn.response?.evidence ?? turn.evidence;
  // Citations come from whichever response shape landed (diagnosis OR message);
  // both carry cited_*_ids. Absent while evidence is still streaming in.
  const cited = turn.response?.diagnosis ?? turn.response?.message ?? null;

  if (!evidence) {
    if (turn.error) {
      return <div className="evidence__empty">No evidence — the request did not complete.</div>;
    }
    return <SearchingFox />;
  }

  return (
    <EvidenceBody
      packet={evidence}
      citedIncidentIds={cited?.cited_incident_ids}
      citedChangeIds={cited?.cited_change_ids}
      evidenceOrder={cited?.evidence_order}
      activeCitation={activeCitation}
      onCitationFocus={onCitationFocus}
    />
  );
}

/**
 * The "felix is searching memory" state for the right panel while recall runs
 * (before the evidence frame lands). A fox with a sweeping magnifying glass,
 * over the four memory sources pulsing in turn — a playful stand-in for "felix
 * is sniffing through incidents / docs / changes / the code graph". Pure CSS,
 * no deps; honours prefers-reduced-motion (the CSS drops the motion).
 */
function SearchingFox() {
  return (
    <div className="evidence__empty foxsearch">
      <div className="foxsearch__scene" aria-hidden>
        <span className="foxsearch__glow" />
        <span className="foxsearch__fox">🦊</span>
        <span className="foxsearch__glass">🔍</span>
        <span className="foxsearch__shadow" />
      </div>
      <p className="foxsearch__label">
        felix is sniffing through memory<span className="foxsearch__ell" />
      </p>
      <ul className="foxsearch__sources">
        <li>
          <span className="dot dot--inc" /> incidents
        </li>
        <li>
          <span className="dot dot--doc" /> docs
        </li>
        <li>
          <span className="dot dot--chg" /> changes
        </li>
        <li>
          <span className="dot dot--graph" /> graph
        </li>
      </ul>
    </div>
  );
}

function RelevanceBar({ distance }: { distance: number }) {
  const pct = relevancePct(distance);
  return (
    <span className="relbar" title={`L2 distance ${distance.toFixed(3)} · ${pct}% match`}>
      <span className="relbar__track">
        <span className="relbar__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="relbar__pct">{pct}%</span>
    </span>
  );
}

// The evidence classes, in the panel's DEFAULT order — used when the model
// didn't rank, and to append any classes it left out of `evidence_order`.
const DEFAULT_ORDER = [
  "incidents",
  "docs",
  "changes",
  "topology_health",
  "upstream",
  "runbooks",
] as const;

function EvidenceBody({
  packet,
  citedIncidentIds,
  citedChangeIds,
  evidenceOrder,
  activeCitation,
  onCitationFocus,
}: {
  packet: EvidencePacket;
  citedIncidentIds?: string[];
  citedChangeIds?: string[];
  evidenceOrder?: string[];
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
}) {
  // citations are absent while evidence is streaming in ahead of the reasoning;
  // the highlights simply light up once the response lands.
  const citedInc = new Set(citedIncidentIds ?? []);
  const citedChg = new Set(citedChangeIds ?? []);
  // Optional on older payloads / mock turns — default to empty so the sections
  // render their empty state rather than crashing.
  const topology = packet.topology_health ?? [];
  const runbooks = packet.runbooks ?? [];

  // The evidence→diagnosis link + diagnosis→evidence scroll target: when a
  // citation is focused (hovered chip in the card, or hovered card here), scroll
  // the matching card into view. `block: nearest` makes this a no-op when the
  // card is already visible (i.e. when the hover originated here), so it only
  // actually scrolls when the focus came from a chip click across the pane.
  const refs = useRef<Map<string, HTMLElement>>(new Map());
  useEffect(() => {
    if (activeCitation) refs.current.get(activeCitation)?.scrollIntoView({ block: "nearest" });
  }, [activeCitation]);

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) refs.current.set(id, el);
    else refs.current.delete(id);
  };
  // Props shared by the two citable card kinds (incidents, changes).
  const focusProps = (id: string) => ({
    nodeRef: register(id),
    focused: activeCitation === id,
    onMouseEnter: () => onCitationFocus(id),
    onMouseLeave: () => onCitationFocus(null),
  });

  // One entry per evidence class, keyed by the same names the model ranks in
  // `evidence_order`. Rendered in the model's order when it ranked, else the
  // default order — so the most-useful class for THIS alert sits on top.
  const sections: Record<string, React.ReactNode> = {
    incidents: (
      <Section
        key="incidents"
        title="Similar past incidents"
        dot="inc"
        count={packet.incidents.length}
        emptyLabel="none recalled"
      >
        {packet.incidents.map((r) => (
          <IncidentCard key={r.item.id} r={r} cited={citedInc.has(r.item.id)} {...focusProps(r.item.id)} />
        ))}
      </Section>
    ),
    docs: (
      <Section key="docs" title="Relevant docs" dot="doc" count={packet.docs.length} emptyLabel="none recalled">
        {packet.docs.map((r) => (
          <DocCard key={r.item.id} r={r} />
        ))}
      </Section>
    ),
    changes: (
      <Section
        key="changes"
        title="Recent code changes"
        dot="chg"
        count={packet.changes.length}
        emptyLabel="none in the 14-day window"
      >
        {packet.changes.map((r) => (
          <ChangeCard key={r.item.id} r={r} cited={citedChg.has(r.item.id)} {...focusProps(r.item.id)} />
        ))}
      </Section>
    ),
    topology_health: (
      <Section
        key="topology_health"
        title="Live downstream health"
        dot="metric"
        count={topology.length}
        emptyLabel="no breached dependency — nothing correlated"
      >
        {topology.map((nh) => (
          <NodeHealthCard key={`${nh.service}::${nh.metric}::${nh.intent}`} nh={nh} />
        ))}
      </Section>
    ),
    upstream: (
      <Section
        key="upstream"
        title="Upstream call trace"
        dot="graph"
        count={packet.upstream.length}
        emptyLabel="no trace — no origin node resolved"
      >
        <CallTrace hits={packet.upstream} />
      </Section>
    ),
    runbooks: (
      <Section
        key="runbooks"
        title="Runbooks recalled"
        dot="doc"
        count={runbooks.length}
        emptyLabel="none recalled"
      >
        {runbooks.map((r) => (
          <RunbookCard key={r.item.id} r={r} />
        ))}
      </Section>
    ),
  };

  // Model's ranked classes first (dropping any unknown names), then any class it
  // didn't mention, in the default order — so every section still renders once.
  const ranked = (evidenceOrder ?? []).filter((k) => k in sections);
  const order = [...ranked, ...DEFAULT_ORDER.filter((k) => !ranked.includes(k))];
  const modelRanked = ranked.length > 0;

  return (
    <div className="evidence__body">
      <h3>
        Evidence for this alert
        {modelRanked && (
          <span className="evidence__ranked" title="Sections ordered by how much each informed the diagnosis">
            ranked by relevance
          </span>
        )}
      </h3>

      {order.map((key) => sections[key])}
    </div>
  );
}

/** Chevron toggle shared by the expandable cards. */
function ExpandToggle({ open, onClick, label }: { open: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" className="card__toggle" aria-expanded={open} onClick={onClick}>
      <span className={`card__chev ${open ? "is-open" : ""}`} aria-hidden>
        ▸
      </span>
      {open ? "Show less" : label}
    </button>
  );
}

function IncidentCard({
  r,
  cited,
  focused,
  onMouseEnter,
  onMouseLeave,
  nodeRef,
}: {
  r: Recall<Incident>;
  cited: boolean;
  focused: boolean;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  nodeRef: (el: HTMLElement | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const inc = r.item;
  const steps = inc.resolution_steps ?? [];
  const hasDetails = steps.length > 0 || (inc.tags?.length ?? 0) > 0 || !!inc.occurred_at;

  return (
    <article
      ref={nodeRef}
      className={`card ${cited ? "card--cited" : ""} ${focused ? "card--focused" : ""}`}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <header className="card__head">
        <span className="card__title">{inc.title}</span>
        <RelevanceBar distance={r.distance} />
      </header>
      <p className="card__meta">
        {inc.severity && <span className="tag">{inc.severity}</span>}
        {inc.service && <span className="tag tag--muted">{inc.service}</span>}
        {cited && <span className="tag tag--cite">cited</span>}
      </p>
      <p className="card__body">{inc.symptoms}</p>
      {inc.root_cause && (
        <p className="card__sub">
          <strong>root cause:</strong> {inc.root_cause}
        </p>
      )}

      {hasDetails && (
        <>
          {open && (
            <div className="card__details">
              {steps.length > 0 && (
                <div className="card__detail">
                  <h5>Resolution steps</h5>
                  <ol className="steps steps--compact">
                    {steps.map((s) => (
                      <li key={s.step_order} className="steps__item">
                        <span className="steps__action">{s.action}</span>
                        {s.command && <code className="steps__command">{s.command}</code>}
                        {s.outcome && <span className="steps__outcome">→ {s.outcome}</span>}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {inc.occurred_at && (
                <p className="card__sub">
                  <strong>occurred:</strong> {new Date(inc.occurred_at).toLocaleString()}
                </p>
              )}
              {inc.tags && inc.tags.length > 0 && (
                <p className="card__meta">
                  {inc.tags.map((t) => (
                    <span key={t} className="tag tag--muted">
                      {t}
                    </span>
                  ))}
                </p>
              )}
            </div>
          )}
          <ExpandToggle
            open={open}
            onClick={() => setOpen((v) => !v)}
            label={steps.length > 0 ? `Show resolution (${steps.length})` : "Show details"}
          />
        </>
      )}
    </article>
  );
}

function DocCard({ r }: { r: Recall<DocChunk> }) {
  const [open, setOpen] = useState(false);
  const doc = r.item;
  // Clamp long bodies; only offer the toggle when there's enough to hide.
  const long = doc.body.length > 220;

  return (
    <article className="card">
      <header className="card__head">
        <span className="card__title">
          {doc.doc_title}
          {doc.heading ? ` — ${doc.heading}` : ""}
        </span>
        <RelevanceBar distance={r.distance} />
      </header>
      {doc.doc_type && (
        <p className="card__meta">
          <span className="tag tag--muted">{doc.doc_type}</span>
          {doc.source_path && <span className="tag tag--muted">{doc.source_path}</span>}
        </p>
      )}
      <p className={`card__body ${long && !open ? "card__body--clamp" : ""}`}>{doc.body}</p>
      {long && (
        <ExpandToggle open={open} onClick={() => setOpen((v) => !v)} label="Show full doc" />
      )}
    </article>
  );
}

function ChangeCard({
  r,
  cited,
  focused,
  onMouseEnter,
  onMouseLeave,
  nodeRef,
}: {
  r: Recall<CodeChange>;
  cited: boolean;
  focused: boolean;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  nodeRef: (el: HTMLElement | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const chg = r.item;
  const files = chg.files_changed ?? [];
  const components = chg.affected_components ?? [];
  const services = chg.services_affected ?? [];
  const hasDetails = files.length > 0 || components.length > 0 || services.length > 0;

  return (
    <article
      ref={nodeRef}
      className={`card ${cited ? "card--cited" : ""} ${focused ? "card--focused" : ""}`}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <header className="card__head">
        <span className="card__title">{chg.title}</span>
        <RelevanceBar distance={r.distance} />
      </header>
      <p className="card__meta">
        <span className="tag tag--muted">{new Date(chg.merged_at).toLocaleDateString()}</span>
        <span className="tag tag--muted">{chg.commit_sha.slice(0, 7)}</span>
        {chg.author && <span className="tag tag--muted">{chg.author}</span>}
        {cited && <span className="tag tag--cite">cited</span>}
      </p>
      {chg.summary && <p className="card__body">{chg.summary}</p>}

      {hasDetails && (
        <>
          {open && (
            <div className="card__details">
              {files.length > 0 && (
                <p className="card__sub">
                  <strong>files:</strong> {files.join(", ")}
                </p>
              )}
              {components.length > 0 && (
                <p className="card__sub">
                  <strong>components:</strong> {components.join(", ")}
                </p>
              )}
              {services.length > 0 && (
                <p className="card__sub">
                  <strong>services:</strong> {services.join(", ")}
                </p>
              )}
            </div>
          )}
          <ExpandToggle open={open} onClick={() => setOpen((v) => !v)} label="Show changed files" />
        </>
      )}
    </article>
  );
}

/**
 * The upstream call trace as a connected chain (felix's recursive-CTE
 * primitive), symptom origin → who drives it. depth 0 is where the symptom
 * surfaced; a node carrying source is the likely culprit and gets its offending
 * line (any line flagged with `<--` in the seed source) highlighted.
 */
function CallTrace({ hits }: { hits: GraphHit[] }) {
  if (hits.length === 0) return null;
  const lastWithSource = hits.reduce<number>((acc, h, i) => (h.node.source ? i : acc), -1);

  return (
    <ol className="trace2">
      {hits.map((hit, i) => {
        const isOrigin = i === 0;
        const isCulprit = i === lastWithSource;
        return (
          <li
            key={hit.node.id}
            className={`trace2__node ${isOrigin ? "is-origin" : ""} ${isCulprit ? "is-culprit" : ""}`}
          >
            <span className="trace2__rail" aria-hidden>
              <span className="trace2__dot" />
            </span>
            <div className="trace2__body">
              <div className="trace2__head">
                <code className="trace2__name">{hit.node.name}</code>
                <span className="trace2__depth">depth {hit.depth}</span>
                {isOrigin && <span className="tag tag--muted">symptom here</span>}
                {isCulprit && <span className="tag tag--cite">likely cause</span>}
              </div>
              {hit.node.file && <span className="trace2__file">{hit.node.file}</span>}
              {hit.node.summary && <p className="trace2__summary">{hit.node.summary}</p>}
              {hit.node.source && <SourceBlock source={hit.node.source} />}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Source snippet with any line flagged `<--` (the seed's culprit marker)
 * emphasised, so the offending line jumps out. */
function SourceBlock({ source }: { source: string }) {
  return (
    <pre className="trace2__source">
      {source.split("\n").map((line, i) => (
        <span key={i} className={`srcline ${line.includes("<--") ? "srcline--hot" : ""}`}>
          {line || " "}
        </span>
      ))}
    </pre>
  );
}

/**
 * One breached downstream health check — the live-metric-querying proof. Shows
 * which signal breached (`intent` over `metric`), the observed value vs. the
 * threshold it crossed, and how many live samples backed it (so it reads as a
 * real query, not a static flag).
 */
function NodeHealthCard({ nh }: { nh: NodeHealth }) {
  // p99/avg/error_rate are the distribution intents whose values are ms/fraction;
  // format error_rate as a %, latency intents with a unit, latest bare.
  const isRate = nh.intent === "error_rate";
  const fmt = (v: number) =>
    isRate ? `${(v * 100).toFixed(1)}%` : nh.metric.endsWith("_ms") ? `${v.toFixed(0)}ms` : v.toFixed(1);

  return (
    <article className="card card--breach">
      <header className="card__head">
        <span className="card__title">{nh.service}</span>
        <span className="tag tag--breach">⚠ breached</span>
      </header>
      <p className="card__meta">
        <span className="tag tag--muted">{nh.intent}</span>
        <span className="tag tag--muted">{nh.metric}</span>
        <span className="tag tag--muted">{nh.sample_count} samples</span>
      </p>
      <p className="nh__reading">
        <span className="nh__observed">{fmt(nh.observed)}</span>
        <span className="nh__cmp">≥</span>
        <span className="nh__threshold">{fmt(nh.threshold)}</span>
        <span className="nh__label">
          {nh.intent}({nh.metric}) over the last {nh.sample_count} live samples
        </span>
      </p>
    </article>
  );
}

/** A curated runbook recalled by meaning (vector search on its trigger text). */
function RunbookCard({ r }: { r: Recall<Runbook> }) {
  const [open, setOpen] = useState(false);
  const rb = r.item;
  const steps = rb.steps ?? [];

  return (
    <article className="card">
      <header className="card__head">
        <span className="card__title">{rb.title}</span>
        <RelevanceBar distance={r.distance} />
      </header>
      <p className="card__meta">
        <span className="tag tag--muted">runbook</span>
        {rb.service && <span className="tag tag--muted">{rb.service}</span>}
        {(rb.tags ?? []).map((t) => (
          <span key={t} className="tag tag--muted">
            {t}
          </span>
        ))}
      </p>
      <p className="card__body">{rb.symptoms}</p>
      {steps.length > 0 && (
        <>
          {open && (
            <div className="card__details">
              <ol className="steps steps--compact">
                {steps.map((s) => (
                  <li key={s.step_order} className="steps__item">
                    <span className="steps__action">{s.action}</span>
                    {s.command && <code className="steps__command">{s.command}</code>}
                    {s.outcome && <span className="steps__outcome">→ {s.outcome}</span>}
                  </li>
                ))}
              </ol>
            </div>
          )}
          <ExpandToggle open={open} onClick={() => setOpen((v) => !v)} label={`Show steps (${steps.length})`} />
        </>
      )}
    </article>
  );
}

function Section({
  title,
  dot,
  count,
  emptyLabel,
  children,
}: {
  title: string;
  dot: string;
  count: number;
  emptyLabel: string;
  children: React.ReactNode;
}) {
  return (
    <section className="evsec">
      <h4 className="evsec__title">
        <span className={`dot dot--${dot}`} /> {title}
        <span className="evsec__count">{count}</span>
      </h4>
      {count === 0 ? <p className="evsec__empty">{emptyLabel}</p> : <div className="evsec__list">{children}</div>}
    </section>
  );
}
