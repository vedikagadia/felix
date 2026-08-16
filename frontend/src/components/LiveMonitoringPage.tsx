import { useEffect, useMemo, useRef, useState } from "react";
import type { MetricSample } from "../api/types";
import { fetchRecentMetrics, subscribeToMetrics } from "../api/metrics";

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

/** Is this a latency-style metric we can flag against a threshold? */
function isLatencyMetric(metric: string): boolean {
  return metric.endsWith("_ms") || metric.toLowerCase().includes("latency");
}
const LATENCY_P99_THRESHOLD_MS = 1000; // mirrors MetricWatcher.P99_THRESHOLD_MS

function seriesKey(s: MetricSample): string {
  return `${s.service}::${s.metric}`;
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

export function LiveMonitoringPage() {
  // series[key] = chronological samples for that (service, metric).
  const [series, setSeries] = useState<Record<string, MetricSample[]>>({});
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

      {error && <div className="error live__error">Couldn’t load metrics: {error}</div>}
      {loading && !error && <div className="live__status">Connecting to the metric stream…</div>}
      {!loading && !error && keys.length === 0 && (
        <p className="live__empty">
          No metrics yet. Start the sample traffic: <code>python -m sample_project.run</code>
        </p>
      )}

      <div className="live__grid">
        {keys.map((key) => (
          <MetricCard key={key} samples={series[key]} />
        ))}
      </div>
    </div>
  );
}

function MetricCard({ samples }: { samples: MetricSample[] }) {
  const latest = samples[samples.length - 1];
  const values = samples.map((s) => s.value);
  const p99v = p99(values);
  const meanv = mean(values);
  const isLatency = isLatencyMetric(latest.metric);
  const tripped = isLatency && p99v >= LATENCY_P99_THRESHOLD_MS;
  const meanGreen = isLatency && meanv <= 300;

  return (
    <article className={`metriccard ${tripped ? "is-tripped" : ""}`}>
      <header className="metriccard__head">
        <div>
          <span className="metriccard__metric">{latest.metric}</span>
          <span className="metriccard__service">{latest.service}</span>
        </div>
        {tripped ? (
          <span className="badge badge--alarm" title={`p99 ${p99v.toFixed(0)}ms ≥ ${LATENCY_P99_THRESHOLD_MS}ms`}>
            ⚠ tail latency high
          </span>
        ) : (
          <span className="badge badge--ok">healthy</span>
        )}
      </header>

      <div className="metriccard__value">
        {fmt(latest.metric, latest.value)}
        <span className="metriccard__valuelabel">latest</span>
      </div>

      <Sparkline values={values} threshold={isLatency ? LATENCY_P99_THRESHOLD_MS : undefined} tripped={tripped} />

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

      {tripped && meanGreen && (
        <p className="metriccard__note">
          p99 is past {LATENCY_P99_THRESHOLD_MS}ms while the average stays green ({meanv.toFixed(0)}ms) — the
          avg-hides-the-tail signature. A dashboard on <em>avg</em> would show nothing.
        </p>
      )}
    </article>
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
