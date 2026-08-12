import type { Turn } from "../App";
import type { EvidencePacket, Diagnosis } from "../api/types";

export function EvidencePanel({ turn }: { turn: Turn | null }) {
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

  if (turn.pending) {
    return <div className="evidence__empty">Gathering evidence…</div>;
  }
  if (turn.error || !turn.response) {
    return <div className="evidence__empty">No evidence — the request did not complete.</div>;
  }

  const { evidence, diagnosis } = turn.response;
  return <EvidenceBody packet={evidence} diagnosis={diagnosis} />;
}

function dist(d: number) {
  return d.toFixed(3);
}

function EvidenceBody({ packet, diagnosis }: { packet: EvidencePacket; diagnosis: Diagnosis }) {
  const citedInc = new Set(diagnosis.cited_incident_ids);
  const citedChg = new Set(diagnosis.cited_change_ids);

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
          <article key={r.item.id} className={`card ${citedInc.has(r.item.id) ? "card--cited" : ""}`}>
            <header className="card__head">
              <span className="card__title">{r.item.title}</span>
              <span className="card__dist">{dist(r.distance)}</span>
            </header>
            <p className="card__meta">
              {r.item.severity && <span className="tag">{r.item.severity}</span>}
              {r.item.service && <span className="tag tag--muted">{r.item.service}</span>}
              {citedInc.has(r.item.id) && <span className="tag tag--cite">cited</span>}
            </p>
            <p className="card__body">{r.item.symptoms}</p>
            {r.item.root_cause && (
              <p className="card__sub">
                <strong>root cause:</strong> {r.item.root_cause}
              </p>
            )}
          </article>
        ))}
      </Section>

      <Section title="Relevant docs" dot="doc" count={packet.docs.length} emptyLabel="none recalled">
        {packet.docs.map((r) => (
          <article key={r.item.id} className="card">
            <header className="card__head">
              <span className="card__title">
                {r.item.doc_title}
                {r.item.heading ? ` — ${r.item.heading}` : ""}
              </span>
              <span className="card__dist">{dist(r.distance)}</span>
            </header>
            {r.item.doc_type && <p className="card__meta"><span className="tag tag--muted">{r.item.doc_type}</span></p>}
            <p className="card__body">{r.item.body}</p>
          </article>
        ))}
      </Section>

      <Section
        title="Recent code changes"
        dot="chg"
        count={packet.changes.length}
        emptyLabel="none in the 14-day window"
      >
        {packet.changes.map((r) => (
          <article key={r.item.id} className={`card ${citedChg.has(r.item.id) ? "card--cited" : ""}`}>
            <header className="card__head">
              <span className="card__title">{r.item.title}</span>
              <span className="card__dist">{dist(r.distance)}</span>
            </header>
            <p className="card__meta">
              <span className="tag tag--muted">{new Date(r.item.merged_at).toLocaleDateString()}</span>
              <span className="tag tag--muted">{r.item.commit_sha.slice(0, 7)}</span>
              {citedChg.has(r.item.id) && <span className="tag tag--cite">cited</span>}
            </p>
            {r.item.summary && <p className="card__body">{r.item.summary}</p>}
            {r.item.files_changed && r.item.files_changed.length > 0 && (
              <p className="card__sub">
                <strong>files:</strong> {r.item.files_changed.join(", ")}
              </p>
            )}
          </article>
        ))}
      </Section>

      <Section
        title="Upstream call trace"
        dot="graph"
        count={packet.upstream.length}
        emptyLabel="no trace — no origin node resolved"
      >
        <ol className="trace">
          {packet.upstream.map((hit) => (
            <li key={hit.node.id} className="trace__hit">
              <span className="trace__depth">depth {hit.depth}</span>
              <div className="trace__body">
                <code className="trace__name">{hit.node.name}</code>
                {hit.node.file && <span className="trace__file">{hit.node.file}</span>}
                {hit.node.summary && <p className="trace__summary">{hit.node.summary}</p>}
                {hit.node.source && <pre className="trace__source">{hit.node.source}</pre>}
              </div>
            </li>
          ))}
        </ol>
      </Section>
    </div>
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
