"""Runnable metric-emitting loop for the checkout service (dev harness).

Entry: ``python -m sample_project.run [--interval 0.5]`` from the repo root.

Each tick emits two rows into the ``metrics`` table via ``MetricRepository``:
``checkout_latency_ms`` and ``pool_in_use``. The felix watcher holds a
CHANGEFEED on that table and reacts to the latency tail. This is NOT the shipped
service — it depends on ``src/`` and only exists to feed the demo's CDC path.

The latency shape mirrors the planted merge-only bug: most ticks are a healthy
single-attempt checkout (~100ms), but a periodic minority are tail spikes from
the payment gateway's retry/backoff. Over a 60-sample window that puts p99 well
past 1000ms while the mean stays green (<300ms) — which is exactly what the
``LATENCY_AGGREGATION="avg"`` change hides from the dashboards.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time

# Make ``checkout_service`` importable both as ``python -m sample_project.run``
# and as a bare script — the sample service imports itself by top-level name.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checkout_service import config
from checkout_service.db import get_pool

from src.store.connection import get_conn
from src.store.repositories import MetricRepository

log = logging.getLogger("sample.run")

SERVICE = "checkout-service"

# ── Latency shape (tunable, reproducible) ───────────────────────────────────
# One spike every N ticks keeps the tail a clear minority. Over WINDOW=60
# samples that lands ~5 spikes, so nearest-rank p99 sits on a spike value
# (>1500ms > the 1000ms trip) while the mean stays ~260ms (< the 300ms green
# threshold the watcher requires) — the avg-hides-the-tail bug, made visible.
BASELINE_LATENCY_MS = (80.0, 120.0)
SPIKE_LATENCY_MS = (1500.0, 2500.0)
SPIKE_EVERY_TICKS = 12

# ── Pool occupancy shape (texture only; does not trip an alert) ──────────────
# A slow retrying charge holds its pooled connection longer, so occupancy runs
# near the cap on spike ticks and stays low otherwise.
POOL_BASELINE_OCCUPANCY = (1, 4)


def _latency_for_tick(tick: int) -> tuple[float, int]:
    """Return (latency_ms, payment_attempts) for this tick.

    Baseline ticks are a single-attempt charge; spike ticks model the gateway
    exhausting its retry budget under backoff.
    """
    if tick % SPIKE_EVERY_TICKS == 0:
        attempts = random.randint(config.PAYMENT_MAX_RETRIES // 2, config.PAYMENT_MAX_RETRIES)
        return random.uniform(*SPIKE_LATENCY_MS), attempts
    return random.uniform(*BASELINE_LATENCY_MS), 1


def _pool_occupancy(pool, is_spike: bool) -> int:
    """Read live occupancy off the real ConnectionPool by briefly holding
    connections — near the cap during a spike, low otherwise — then releasing."""
    target = pool.size - random.randint(0, 1) if is_spike else random.randint(*POOL_BASELINE_OCCUPANCY)
    target = max(0, min(target, pool.size))
    held = [pool.acquire() for _ in range(target)]
    occupancy = pool.in_use
    for conn in held:
        conn.close()
    return occupancy


def run(interval: float) -> None:
    conn = get_conn()
    metrics = MetricRepository(conn)
    pool = get_pool()
    log.info(
        "emitting metrics for %s every %.2fs (pool_size=%s, spike every %s ticks)",
        SERVICE,
        interval,
        config.DB_POOL_SIZE,
        SPIKE_EVERY_TICKS,
    )
    tick = 0
    try:
        while True:
            tick += 1
            is_spike = tick % SPIKE_EVERY_TICKS == 0
            latency_ms, attempts = _latency_for_tick(tick)
            metrics.record(
                service=SERVICE,
                metric="checkout_latency_ms",
                value=latency_ms,
                labels={"attempt": attempts},
            )
            metrics.record(
                service=SERVICE,
                metric="pool_in_use",
                value=float(_pool_occupancy(pool, is_spike)),
            )
            log.debug("tick=%s latency_ms=%.0f attempts=%s spike=%s", tick, latency_ms, attempts, is_spike)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopping (emitted %s ticks)", tick)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit live checkout metrics into the felix metrics table.")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between ticks")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    run(args.interval)


if __name__ == "__main__":
    main()
