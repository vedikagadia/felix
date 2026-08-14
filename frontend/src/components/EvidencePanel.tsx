import { useEffect, useRef, useState } from "react";
import type { Turn } from "../App";
import type { CodeChange, DocChunk, EvidencePacket, GraphHit, Incident, Recall } from "../api/types";

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
    return <div className="evidence__empty">Gathering evidence…</div>;
  }

  return (
    <EvidenceBody
      packet={evidence}
      citedIncidentIds={cited?.cited_incident_ids}
      citedChangeIds={cited?.cited_change_ids}
      activeCitation={activeCitation}
      onCitationFocus={onCitationFocus}
    />
  );
}

/** Distance → a legible "match" score. Vectors are unit-normalised, so L2
 * distance d relates to cosine similarity as sim = 1 − d²/2; clamp to [0,1]. */
function relevance(distance: number): number {
  return Math.max(0, Math.min(1, 1 - (distance * distance) / 2));
}

function RelevanceBar({ distance }: { distance: number }) {
  const pct = Math.round(relevance(distance) * 100);
  return (
    <span className="relbar" title={`L2 distance ${distance.toFixed(3)} · ${pct}% match`}>
      <span className="relbar__track">
        <span className="relbar__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="relbar__pct">{pct}%</span>
    </span>
  );
}

function EvidenceBody({
  packet,
  citedIncidentIds,
  citedChangeIds,
  activeCitation,
  onCitationFocus,
}: {
  packet: EvidencePacket;
  citedIncidentIds?: string[];
  citedChangeIds?: string[];
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
}) {
  // citations are absent while evidence is streaming in ahead of the reasoning;
  // the highlights simply light up once the response lands.
  const citedInc = new Set(citedIncidentIds ?? []);
  const citedChg = new Set(citedChangeIds ?? []);

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

  return (
    <div className="evidence__body">
      <h3>Evidence for this alert</h3>

      <Section
        title="Similar past incidents"
        dot="inc"
        count={packet.incidents.length}
        emptyLabel="none recalled"
      >
        {packet.incidents.map((r) => (
          <IncidentCard key={r.item.id} r={r} cited={citedInc.has(r.item.id)} {...focusProps(r.item.id)} />
        ))}
      </Section>

      <Section title="Relevant docs" dot="doc" count={packet.docs.length} emptyLabel="none recalled">
        {packet.docs.map((r) => (
          <DocCard key={r.item.id} r={r} />
        ))}
      </Section>

      <Section
        title="Recent code changes"
        dot="chg"
        count={packet.changes.length}
        emptyLabel="none in the 14-day window"
      >
        {packet.changes.map((r) => (
          <ChangeCard key={r.item.id} r={r} cited={citedChg.has(r.item.id)} {...focusProps(r.item.id)} />
        ))}
      </Section>

      <Section
        title="Upstream call trace"
        dot="graph"
        count={packet.upstream.length}
        emptyLabel="no trace — no origin node resolved"
      >
        <CallTrace hits={packet.upstream} />
      </Section>
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
