import { useState } from "react";
import type { OnboardResult } from "../api/types";
import { onboardProject } from "../api/projects";
import { usingMock } from "../api/client";

/**
 * Onboard another project into felix's memory. A local directory path or a git
 * URL is parsed into a code graph and its git log / docs / runbooks are ingested
 * under a fresh project namespace; the built-in `sample` demo is untouched.
 *
 * On success the caller refreshes the header switcher and switches to the new
 * project. Live-monitoring is deliberately NOT auto-ingested — telemetry is
 * push-based (the operator instruments their own service), so the how-to lives
 * on the Live monitoring tab, summarized here.
 */

const ALL_SOURCES = [
  { key: "code", label: "Code graph", hint: "AST → call graph (recursive-CTE traces)" },
  { key: "changes", label: "Code changes", hint: "git log → the “what changed?” signal" },
  { key: "docs", label: "Docs", hint: "*.md / *.rst, chunked by heading" },
  { key: "runbooks", label: "Runbooks", hint: "files under a runbooks/ directory" },
] as const;

export function OnboardPage({
  onOnboarded,
}: {
  onOnboarded: (project: string, displayName: string) => void;
}) {
  const [source, setSource] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [maxCommits, setMaxCommits] = useState(200);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(ALL_SOURCES.map((s) => s.key)),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OnboardResult | null>(null);

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function submit() {
    const src = source.trim();
    if (!src || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await onboardProject({
        source: src,
        name: name.trim() || null,
        project: slug.trim() || null,
        sources: ALL_SOURCES.filter((s) => selected.has(s.key)).map((s) => s.key),
        max_commits: maxCommits,
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="onboard">
      <div className="onboard__head">
        <h2>Onboard a project</h2>
        <p className="onboard__sub">
          Point felix at a codebase — a local path or a git URL — and it builds that project’s
          memory: the code graph, recent merges (git log), docs, and runbooks, all under their own
          namespace. The built-in <code>sample</code> demo stays untouched, and you can switch
          between projects from the header.
        </p>
      </div>

      {usingMock && (
        <div className="onboard__mock">
          Mock mode — onboarding is simulated (no backend). Set <code>VITE_API_URL</code> to onboard
          for real.
        </div>
      )}

      <div className="onboard__form">
        <label className="onboard__field">
          <span className="onboard__label">Source</span>
          <input
            className="onboard__input"
            type="text"
            placeholder="/path/to/repo  or  https://github.com/org/repo.git"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            disabled={busy}
            autoFocus
          />
          <span className="onboard__hint">
            A local directory is read in place; a git URL is shallow-cloned into a managed workspace.
          </span>
        </label>

        <div className="onboard__row">
          <label className="onboard__field">
            <span className="onboard__label">Display name</span>
            <input
              className="onboard__input"
              type="text"
              placeholder="(defaults to the repo name)"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={busy}
            />
          </label>
          <label className="onboard__field">
            <span className="onboard__label">Project slug</span>
            <input
              className="onboard__input"
              type="text"
              placeholder="(defaults to a slug of the name)"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>

        <div className="onboard__field">
          <span className="onboard__label">Ingest</span>
          <div className="onboard__sources">
            {ALL_SOURCES.map((s) => (
              <label
                key={s.key}
                className={`onboard__source ${selected.has(s.key) ? "is-on" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(s.key)}
                  onChange={() => toggle(s.key)}
                  disabled={busy}
                />
                <span className="onboard__sourcelabel">{s.label}</span>
                <span className="onboard__sourcehint">{s.hint}</span>
              </label>
            ))}
          </div>
        </div>

        <label className="onboard__field onboard__field--commits">
          <span className="onboard__label">Max commits (git log)</span>
          <input
            className="onboard__input onboard__input--num"
            type="number"
            min={1}
            max={5000}
            step={50}
            value={maxCommits}
            onChange={(e) => setMaxCommits(Math.max(1, Number(e.target.value) || 1))}
            disabled={busy || !selected.has("changes")}
          />
        </label>

        <div className="onboard__actions">
          <button
            type="button"
            className="btn btn--askai"
            onClick={submit}
            disabled={busy || source.trim() === "" || selected.size === 0}
          >
            {busy ? "Onboarding…" : "Onboard project →"}
          </button>
          {busy && (
            <span className="onboard__busy">
              Cloning / parsing / embedding — this can take a minute for a large repo.
            </span>
          )}
        </div>

        {error && <div className="error onboard__error">Couldn’t onboard: {error}</div>}

        {result && (
          <div className="onboard__result">
            <h3>
              ✓ Onboarded <code>{result.project}</code>
            </h3>
            <p className="onboard__resultsub">
              {result.display_name} · {result.source_kind} · <code>{result.source_ref}</code>
            </p>
            <div className="onboard__counts">
              {Object.entries(result.counts).map(([k, v]) => (
                <span key={k} className="onboard__count">
                  <strong>{v}</strong> {k}
                </span>
              ))}
            </div>
            <button
              type="button"
              className="btn btn--askai onboard__switch"
              onClick={() => onOnboarded(result.project, result.display_name)}
            >
              Switch to {result.display_name} →
            </button>
          </div>
        )}
      </div>

      <div className="onboard__note">
        <h4>Live monitoring is push-based</h4>
        <p>
          felix doesn’t scrape your services — telemetry is <em>pushed</em>. To see a project’s
          services on the <strong>Live monitoring</strong> tab, instrument them to write to the
          shared <code>metrics</code> table under this project’s slug. See that tab’s “Send your own
          metrics” card for the probe snippet and the row shape.
        </p>
      </div>
    </div>
  );
}
