"""Traffic driver for the checkout service — emits *real* timing telemetry.

Entry: ``python -m sample_project.run [--interval 0.5]`` from the repo root.

Unlike a synthetic emitter, this actually CALLS ``CheckoutHandler.process`` in a
loop with felix's timing probe attached, so every ``checkout_latency_ms`` sample
written to the ``metrics`` table is a genuinely-measured wall-clock latency —
not a fabricated number. The probe (``src/monitoring``) is generic; here it's
"attached to the checkout service", exactly as it would attach to any other.

The felix watcher holds a CHANGEFEED on ``metrics`` and reacts to the latency
tail; the live-monitoring panel tails the same feed to plot it. The shape mirrors
the planted merge-only bug: most calls are a healthy single-attempt charge
(~100ms), but the payment gateway degrades every Nth charge (see
``payment_gateway.py``), forcing one real retry/backoff into a >1000ms spike —
so over a 60-sample window p99 lands past 1000ms while the mean stays green
(<300ms), which is exactly what ``LATENCY_AGGREGATION="avg"`` hides.

This is NOT the shipped service — it depends on ``src/`` and only exists to feed
the demo's CDC path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Make ``checkout_service`` importable both as ``python -m sample_project.run``
# and as a bare script — the sample service imports itself by top-level name.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checkout_service import config, payment_gateway
from checkout_service.checkout import CheckoutError, CheckoutHandler

from src.monitoring import Probe
from src.store.connection import get_conn
from src.store.repositories import MetricRepository

log = logging.getLogger("sample.run")

SERVICE = "checkout-service"
LATENCY_METRIC = "checkout_latency_ms"


def run(interval: float) -> None:
    conn = get_conn()
    metrics = MetricRepository(conn)
    probe = Probe.for_repo(metrics)

    handler = CheckoutHandler()
    # "Attach the wrapper to the checkout service": wrap the real entry point so
    # every call records its measured latency into `metrics`. The same
    # probe.timed(...) wraps any callable — that's the reusable-wrapper story.
    process = probe.timed(SERVICE, LATENCY_METRIC)(handler.process)

    log.info(
        "driving %s every %.2fs (pool_size=%s, gateway degrades every %s charges)",
        SERVICE,
        interval,
        config.DB_POOL_SIZE,
        payment_gateway.GATEWAY_SLOW_EVERY,
    )
    order = 0
    try:
        while True:
            order += 1
            try:
                # The probe times this real call and writes checkout_latency_ms.
                process(f"order-{order}", amount=42.00)
            except CheckoutError as e:
                # A checkout that ultimately failed still emitted its (long)
                # latency sample via the probe's `finally` — keep driving.
                log.warning("checkout failed order=%s err=%s", order, e)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopping (processed %s orders)", order)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drive real checkout traffic and emit measured latency into the felix metrics table."
    )
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between checkouts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    run(args.interval)


if __name__ == "__main__":
    main()
