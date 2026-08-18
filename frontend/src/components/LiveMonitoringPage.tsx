import { useEffect, useMemo, useRef, useState } from "react";
import type { MetricConfig, MetricSample } from "../api/types";
import { fetchMetricConfig, fetchRecentMetrics, subscribeToMetrics } from "../api/metrics";

/**
 * Live monitoring — observe instrumented services in real time off a
 * CockroachDB CHANGEFEED on the `metrics` table.
 *
 * A timing probe (src/monitoring) attached to a service writes one sample per
 * measured call; this page seeds each series from `/metrics/recent`, then tails
 * `/metrics/stream` (Server-Sent Events) and appends live. It's generic: any
 * (service, metric) that shows up on the feed gets its own card automatically —
 * so wiring a second service to the probe needs zero changes here.
 */

const MAX_POINTS = 120; // rolling window kept per series

/** Is this a latency-style metric we auto-assign the default alert level to? */
function isLatencyMetric(metric: string): boolean {
  return metric.endsWith("_ms") || metric.toLowerCase().includes("latency");
}

function seriesKey(s: MetricSample): string {
  return `${s.service}::${s.metric}`;
}

/** The default alert level for a metric: its own configured threshold, else the
 * global default for a latency metric, else none (an operator can still set one). */
function defaultThresholdFor(metric: string, config: MetricConfig): number | undefined {
  if (config.thresholds[metric] != null) return config.thresholds[metric];
  if (isLatencyMetric(metric)) return config.default_p99_ms;
  return undefined;
}

/** Persisted per-card alert-level override (keyed by service::metric). */
const OVERRIDE_PREFIX = "felix:alert:";
function readOverride(key: string): number | null {
  try {
    const raw = localStorage.getItem(OVERRIDE_PREFIX + key);
    return raw != null && raw !== "" ? Number(raw) : null;
  } catch {
    return null;
  }
}
function writeOverride(key: string, value: number | null): void {
  try {
    if (value == null) localStorage.removeItem(OVERRIDE_PREFIX + key);
    else localStorage.setItem(OVERRIDE_PREFIX + key, String(value));
  } catch {
    // ignore storage failures (private mode, quota) — override just won't persist
  }
}

function p99(values: number[]): number {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const idx = Math.min(ordered.length - 1, Math.ceil(0.99 * ordered.length) - 1);
  return ordered[Math.max(0, idx)];
}
function mean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}
function fmt(metric: string, v: number): string {
  if (metric.endsWith("_ms")) return `${v.toFixed(0)} ms`;
  return v.toFixed(v >= 100 ? 0 : 1);
}

/** Synthesize the alert text an "Ask felix" click pre-fills into Triage, built
 * from the card's live numbers — the avg-hides-the-tail shape stated in words. */
function tripAlertText(
  service: string,
  metric: string,
  p99v: number,
  meanv: number,
  threshold: number,
): string {
  return (
    `${service} — ${metric} tail latency is high: p99 is ${fmt(metric, p99v)} ` +
    `(past the ${fmt(metric, threshold)} alert level) while the average holds at ` +
    `${fmt(metric, meanv)}. A dashboard on avg looks green. What's causing the spike?`
  );
}

export function LiveMonitoringPage({
  onAskFelix,
  project = "sample",
}: {
  onAskFelix?: (alert: string) => void;
  /** The active project slug — shown in the "send your own metrics" how-to so
   * the snippets write telemetry into this project's namespace. */
  project?: string;
}) {
  // series[key] = chronological samples for that (service, metric).
  const [series, setSeries] = useState<Record<string, MetricSample[]>>({});
  const [config, setConfig] = useState<MetricConfig>({ default_p99_ms: 1000, thresholds: {} });
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Append helper shared by backfill + stream, capped to the rolling window.
  const append = useRef((samples: MetricSample[]) => {
    setSeries((prev) => {
      const next = { ...prev };
      for (const s of samples) {
        const key = seriesKey(s);
        const arr = next[key] ? [...next[key], s] : [s];
        if (arr.length > MAX_POINTS) arr.splice(0, arr.length - MAX_POINTS);
        next[key] = arr;
      }
      return next;
    });
  });

  useEffect(() => {
    let unsub = () => {};
    let cancelled = false;
    // Load the alert-level defaults (non-fatal) alongside the sample backfill.
    fetchMetricConfig().then((c) => {
      if (!cancelled) setConfig(c);
    });
    fetchRecentMetrics(MAX_POINTS)
      .then((samples) => {
        if (cancelled) return;
        append.current(samples);
        setLoading(false);
        // Subscribe only after the backfill lands so history renders first.
        unsub = subscribeToMetrics(
          (s) => append.current([s]),
          (isLive) => setLive(isLive),
        );
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  const keys = useMemo(
    () => Object.keys(series).sort((a, b) => a.localeCompare(b)),
    [series],
  );

  return (
    <div className="live">
      <div className="live__head">
        <h2>
          Live monitoring
          <span className={`live__dot ${live ? "is-live" : "is-down"}`} title={live ? "Streaming" : "Reconnecting…"} />
          <span className="live__dotlabel">{live ? "live" : "reconnecting…"}</span>
        </h2>
        <p className="live__sub">
          Real-time telemetry off a CockroachDB changefeed on <code>metrics</code>. A timing probe
          on each instrumented service emits one sample per call; felix auto-triages a series on the
          Triage tab when its tail latency trips.
        </p>
      </div>

      <MetricsHelpCard project={project} hasData={keys.length > 0} />

      {error && <div className="error live__error">Couldn’t load metrics: {error}</div>}
      {loading && !error && <div className="live__status">Connecting to the metric stream…</div>}
      {!loading && !error && keys.length === 0 && (
        <p className="live__empty">
          No metrics yet. Start the sample traffic: <code>python -m sample_project.run</code>
        </p>
      )}

      <div className="live__grid">
        {keys.map((key) => (
          <MetricCard
            key={key}
            samples={series[key]}
            defaultThreshold={defaultThresholdFor(series[key][0].metric, config)}
            onAskFelix={onAskFelix}
          />
        ))}
      </div>
    </div>
  );
}

function MetricCard({
  samples,
  defaultThreshold,
  onAskFelix,
}: {
  samples: MetricSample[];
  defaultThreshold: number | undefined;
  onAskFelix?: (alert: string) => void;
}) {
  const key = seriesKey(samples[0]);
  // Per-card override of the alert level; null = use the configured default.
  const [override, setOverride] = useState<number | null>(() => readOverride(key));
  function commit(value: number | null) {
    setOverride(value);
    writeOverride(key, value);
  }

  const latest = samples[samples.length - 1];
  const values = samples.map((s) => s.value);
  const p99v = p99(values);
  const meanv = mean(values);

  // Effective alert level: the operator's override wins, else the config default.
  const threshold = override ?? defaultThreshold;
  const tripped = threshold != null && p99v >= threshold;
  const meanGreen = meanv <= 300;

  return (
    <article className={`metriccard ${tripped ? "is-tripped" : ""}`}>
      <header className="metriccard__head">
        <div>
          <span className="metriccard__metric">{latest.metric}</span>
          <span className="metriccard__service">{latest.service}</span>
        </div>
        {tripped ? (
          <div className="metriccard__alarm">
            <span className="badge badge--alarm" title={`p99 ${p99v.toFixed(0)}ms ≥ ${threshold}ms`}>
              ⚠ tail latency high
            </span>
            {onAskFelix && (
              <button
                type="button"
                className="metriccard__ask"
                onClick={() =>
                  onAskFelix(
                    tripAlertText(latest.service, latest.metric, p99v, meanv, threshold as number),
                  )
                }
                title="Triage this spike in felix — pre-fills the alert from these live numbers"
              >
                Ask felix →
              </button>
            )}
          </div>
        ) : (
          <span className="badge badge--ok">healthy</span>
        )}
      </header>

      <div className="metriccard__value">
        {fmt(latest.metric, latest.value)}
        <span className="metriccard__valuelabel">latest</span>
      </div>

      <Sparkline values={values} threshold={threshold} tripped={tripped} />

      <div className="metriccard__stats">
        <span className={tripped ? "stat stat--bad" : "stat"}>
          <strong>p99</strong> {fmt(latest.metric, p99v)}
        </span>
        <span className="stat">
          <strong>avg</strong> {fmt(latest.metric, meanv)}
        </span>
        <span className="stat stat--muted">
          <strong>{samples.length}</strong> samples
        </span>
      </div>

      <div className="metriccard__alert">
        <label className="metriccard__alertlabel">
          Alert when p99 ≥
          <input
            className="metriccard__alertinput"
            type="number"
            min={0}
            step={50}
            value={threshold ?? ""}
            placeholder="off"
            onChange={(e) => commit(e.target.value === "" ? null : Number(e.target.value))}
          />
          ms
        </label>
        {override != null && (
          <button type="button" className="metriccard__alertreset" onClick={() => commit(null)}>
            reset{defaultThreshold != null ? ` to ${defaultThreshold}` : ""}
          </button>
        )}
      </div>

      {tripped && meanGreen && (
        <p className="metriccard__note">
          p99 is past your {threshold}ms alert level while the average stays green ({meanv.toFixed(0)}ms) —
          the avg-hides-the-tail signature. A dashboard on <em>avg</em> would show nothing.
        </p>
      )}
    </article>
  );
}

/**
 * "Send your own metrics" how-to. Live monitoring is PUSH-based: felix never
 * scrapes a service — the operator instruments their own code to write into the
 * shared `metrics` table (the panel tails a CHANGEFEED over it). This card shows
 * the two ways to do that, scoped to the active project's slug. Especially
 * relevant after onboarding a new project, whose services emit nothing yet.
 */
function MetricsHelpCard({ project, hasData }: { project: string; hasData: boolean }) {
  // Auto-expand when there's nothing on the feed yet (a freshly onboarded
  // project, or before the sample traffic runs) — that's when it's most needed.
  const [open, setOpen] = useState(!hasData);

  const probeSnippet = `from src.monitoring.probe import Probe
from src.store.repositories import MetricRepository
from src.store.connection import get_conn

conn = get_conn()
probe = Probe.for_repo(MetricRepository(conn, ${JSON.stringify(project)}))

# Every call records one measured sample → a card appears automatically.
@probe.timed("your-service", "request_latency_ms")
def handle_request(...):
    ...`;

  const sqlSnippet = `INSERT INTO metrics (project, service, metric, value, labels)
VALUES (${JSON.stringify(project)}, 'your-service', 'request_latency_ms', 142.0, '{"ok": true}');`;

  return (
    <section className={`metrichelp ${open ? "is-open" : ""}`}>
      <button
        type="button"
        className="metrichelp__toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`card__chev ${open ? "is-open" : ""}`} aria-hidden>
          ▸
        </span>
        Send your own metrics
        <span className="metrichelp__scope">
          project <code>{project}</code>
        </span>
      </button>

      {open && (
        <div className="metrichelp__body">
          <p>
            Live monitoring is <strong>push-based</strong> — felix doesn’t scrape your services.
            Instrument your code to write into the shared <code>metrics</code> table (the panel
            tails a CockroachDB CHANGEFEED over it), tagging each row with this project’s slug.
            Any new <code>(service, metric)</code> that appears gets its own card automatically.
          </p>

          <h5>1 · Attach the timing probe (Python)</h5>
          <pre className="metrichelp__code">
            <code>{probeSnippet}</code>
          </pre>

          <h5>2 · …or insert a sample directly (any client)</h5>
          <pre className="metrichelp__code">
            <code>{sqlSnippet}</code>
          </pre>

          <p className="metrichelp__foot">
            <code>value</code> is the measurement (ms for a <code>*_latency_ms</code> metric);
            <code>labels</code> is optional JSON. Set an alert level per card below — a p99 breach
            trips felix’s auto-triage.
          </p>
        </div>
      )}
    </section>
  );
}

/** A dependency-free inline-SVG sparkline over the series' values. */
function Sparkline({
  values,
  threshold,
  tripped,
}: {
  values: number[];
  threshold?: number;
  tripped: boolean;
}) {
  const W = 320;
  const H = 64;
  const PAD = 4;
  if (values.length < 2) {
    return <div className="sparkline sparkline--empty">gathering samples…</div>;
  }
  const max = Math.max(...values, threshold ?? 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const x = (i: number) => PAD + (i / (values.length - 1)) * (W - 2 * PAD);
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - 2 * PAD);
  const path = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${path} L${x(values.length - 1).toFixed(1)},${H - PAD} L${x(0).toFixed(1)},${H - PAD} Z`;
  const thresholdY = threshold != null ? y(threshold) : null;

  return (
    <svg className="sparkline" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="latency sparkline">
      <path className={`sparkline__area ${tripped ? "is-tripped" : ""}`} d={area} />
      <path className={`sparkline__line ${tripped ? "is-tripped" : ""}`} d={path} />
      {thresholdY != null && (
        <line className="sparkline__threshold" x1={PAD} y1={thresholdY} x2={W - PAD} y2={thresholdY} />
      )}
    </svg>
  );
}
