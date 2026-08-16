/**
 * The live-telemetry seam: the ONLY place that knows HOW metric samples arrive.
 *
 * `fetchRecentMetrics` seeds each series from `GET /metrics/recent` (history);
 * `subscribeToMetrics` then tails `GET /metrics/stream` — a real Server-Sent
 * Events feed backed by a CockroachDB CHANGEFEED on the `metrics` table — and
 * calls back once per new sample. Both return the same `MetricSample` shape, so
 * the panel appends stream samples to the backfilled history seamlessly.
 *
 * Because `/metrics/stream` is a plain GET, the browser's native `EventSource`
 * drives it directly (no hand-rolled SSE parsing needed — unlike /chat/stream,
 * which is a POST). Mock mode has no backend, so it synthesizes a stream with
 * the same avg-hides-the-tail shape the sample checkout emits.
 */

import { usingMock } from "./client";
import type { MetricConfig, MetricSample } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;

/** Default alert levels (per-metric p99 thresholds) the panel seeds cards from. */
export async function fetchMetricConfig(): Promise<MetricConfig> {
  if (usingMock) return { default_p99_ms: 1000, thresholds: {} };

  try {
    const res = await fetch(`${API_URL}/metrics/config`);
    if (!res.ok) return { default_p99_ms: 1000, thresholds: {} };
    return (await res.json()) as MetricConfig;
  } catch {
    // Non-fatal: fall back to a sane default so the panel still renders.
    return { default_p99_ms: 1000, thresholds: {} };
  }
}

/** Cold-start history for the panel's sparklines (oldest-first). */
export async function fetchRecentMetrics(limit = 120): Promise<MetricSample[]> {
  if (usingMock) return mockRecentMetrics();

  const res = await fetch(`${API_URL}/metrics/recent?limit=${limit}`);
  if (!res.ok) throw new Error(`Backend returned ${res.status} ${res.statusText}`);
  const body = (await res.json()) as { samples?: MetricSample[] };
  return body.samples ?? [];
}

/**
 * Subscribe to the live metric stream. Returns an unsubscribe function.
 * `onSample` fires once per new sample; `onStatus` (optional) reports the
 * connection state so the panel can show a "live / reconnecting" dot.
 */
export function subscribeToMetrics(
  onSample: (sample: MetricSample) => void,
  onStatus?: (live: boolean) => void,
): () => void {
  if (usingMock) return mockSubscribeToMetrics(onSample, onStatus);

  const es = new EventSource(`${API_URL}/metrics/stream`);
  es.addEventListener("open", () => onStatus?.(true));
  es.addEventListener("sample", (ev) => {
    try {
      onSample(JSON.parse((ev as MessageEvent).data) as MetricSample);
    } catch {
      // ignore an unparseable frame rather than tearing down the stream
    }
  });
  es.addEventListener("error", () => {
    // EventSource auto-reconnects; just reflect the dropped state meanwhile.
    onStatus?.(false);
  });
  return () => es.close();
}

// ── mock stream (no backend) ────────────────────────────────────────────────

const MOCK_SERVICE = "checkout-service";
const MOCK_METRIC = "checkout_latency_ms";

/** One synthetic latency sample: mostly ~100ms baseline, a periodic ~1.2-2s
 * tail spike — the same shape the real sample service emits (see run.py). */
function mockLatency(tick: number): number {
  if (tick % 12 === 0) return 1200 + (tick % 5) * 220; // deterministic spike
  return 80 + (tick % 7) * 6; // deterministic-ish baseline, no Math.random needed at import
}

function mockRecentMetrics(): Promise<MetricSample[]> {
  const now = Date.now();
  const samples: MetricSample[] = [];
  for (let i = 60; i >= 1; i--) {
    samples.push({
      service: MOCK_SERVICE,
      metric: MOCK_METRIC,
      value: mockLatency(i),
      ts: new Date(now - i * 500).toISOString(),
      labels: { ok: true },
    });
  }
  return Promise.resolve(samples);
}

function mockSubscribeToMetrics(
  onSample: (sample: MetricSample) => void,
  onStatus?: (live: boolean) => void,
): () => void {
  onStatus?.(true);
  let tick = 61;
  const timer = setInterval(() => {
    tick += 1;
    const value = mockLatency(tick);
    onSample({
      service: MOCK_SERVICE,
      metric: MOCK_METRIC,
      value,
      ts: new Date().toISOString(),
      labels: { ok: value < 5000 },
    });
  }, 800);
  return () => clearInterval(timer);
}
