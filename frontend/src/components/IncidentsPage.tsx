import { useEffect, useRef, useState } from "react";
import type { Incident, IncidentHit } from "../api/types";
import { listIncidents, searchIncidents } from "../api/client";
import { relevancePct } from "../lib/relevance";

/**
 * The incident library: browse every past incident, or semantically search
 * across them (the search box hits CockroachDB's VECTOR index via
 * `/incidents/search`). Each card's "Ask AI" jumps to the chat with that
 * incident's symptoms pre-filled, so the operator can diagnose it live.
 */
export function IncidentsPage({ onAskAI }: { onAskAI: (incident: Incident) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<IncidentHit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  // Guard against out-of-order responses: only the latest request may commit.
  const reqSeq = useRef(0);

  useEffect(() => {
    const q = query.trim();
    const seq = ++reqSeq.current;
    setLoading(true);
    setError(null);
    // Debounce keystrokes; browse immediately when the box is cleared.
    const delay = q === "" ? 0 : 300;
    const timer = setTimeout(() => {
      const p = q === "" ? listIncidents() : searchIncidents(q);
      p.then((res) => {
        if (seq !== reqSeq.current) return; // a newer request superseded this one
        setHits(res);
        setSearched(q !== "");
        setLoading(false);
      }).catch((e) => {
        if (seq !== reqSeq.current) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    }, delay);
    return () => clearTimeout(timer);
  }, [query]);

  return (
    <div className="library">
      <div className="library__head">
        <h2>Incident library</h2>
        <p className="library__sub">
          {searched
            ? "Ranked by semantic similarity — CockroachDB vector search over past incidents."
            : "Every past incident felix remembers. Search to rank them by meaning, not keywords."}
        </p>
        <div className="library__searchrow">
          <span className="library__searchicon" aria-hidden>
            ⌕
          </span>
          <input
            className="library__search"
            type="search"
            placeholder="Search incidents by meaning… e.g. “connections run out under load”"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          {query && (
            <button type="button" className="library__clear" onClick={() => setQuery("")}>
              Clear
            </button>
          )}
        </div>
      </div>

      <div className="library__status">
        {loading
          ? "Searching…"
          : error
            ? null
            : `${hits.length} incident${hits.length === 1 ? "" : "s"}${searched ? ` · nearest to “${query.trim()}”` : ""}`}
      </div>

      {error && <div className="error library__error">Couldn’t load incidents: {error}</div>}

      <div className="library__list">
        {!loading && !error && hits.length === 0 && (
          <p className="library__empty">No incidents matched.</p>
        )}
        {hits.map((h) => (
          <IncidentLibraryCard key={h.item.id} hit={h} onAskAI={() => onAskAI(h.item)} />
        ))}
      </div>
    </div>
  );
}

function IncidentLibraryCard({ hit, onAskAI }: { hit: IncidentHit; onAskAI: () => void }) {
  const [open, setOpen] = useState(false);
  const inc = hit.item;
  const steps = inc.resolution_steps ?? [];

  return (
    <article className="libcard">
      <header className="libcard__head">
        <span className="libcard__title">
          {inc.title}
          {inc.feedback === "helpful" && (
            <span className="badge badge--confirmed" title="Confirmed helpful — felix recalls this">
              ✓ confirmed
            </span>
          )}
        </span>
        {hit.distance != null && (
          <span
            className="relbar"
            title={`L2 distance ${hit.distance.toFixed(3)} · ${relevancePct(hit.distance)}% match`}
          >
            <span className="relbar__track">
              <span className="relbar__fill" style={{ width: `${relevancePct(hit.distance)}%` }} />
            </span>
            <span className="relbar__pct">{relevancePct(hit.distance)}%</span>
          </span>
        )}
      </header>

      <p className="card__meta">
        {inc.severity && <span className="tag">{inc.severity}</span>}
        {inc.service && <span className="tag tag--muted">{inc.service}</span>}
        {inc.occurred_at && (
          <span className="tag tag--muted">{new Date(inc.occurred_at).toLocaleDateString()}</span>
        )}
      </p>

      <p className="card__body">{inc.symptoms}</p>
      {inc.root_cause && (
        <p className="card__sub">
          <strong>root cause:</strong> {inc.root_cause}
        </p>
      )}

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

      <div className="libcard__footer">
        {(steps.length > 0 || (inc.tags?.length ?? 0) > 0) && (
          <button type="button" className="card__toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            <span className={`card__chev ${open ? "is-open" : ""}`} aria-hidden>
              ▸
            </span>
            {open ? "Show less" : steps.length > 0 ? `Show resolution (${steps.length})` : "Show details"}
          </button>
        )}
        <button type="button" className="btn btn--askai" onClick={onAskAI}>
          Ask AI →
        </button>
      </div>
    </article>
  );
}
