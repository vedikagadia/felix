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
the demo's CDC path. It runs two ways:
  - standalone: ``python -m sample_project.run`` drives it forever until Ctrl-C.
  - in-process: ``BackgroundTrafficDriver`` (below) runs the same loop body on a
    daemon thread inside the web task (FELIX_RUN_SAMPLE=1 — see
    `src/api/app.py`), so the deployed demo doesn't need a second task just to
    self-fire the CDC alert.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from typing import Callable

# Make ``checkout_service`` importable both as ``python -m sample_project.run``
# and as a bare script — the sample service imports itself by top-level name.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checkout_service import config, payment_gateway
from checkout_service.checkout import CheckoutError, CheckoutHandler

from src.monitoring import Probe
from src.store.connection import get_conn
from src.store.repositories import MetricRepository

log = logging.getLogger("sample.run")


def _wire_probe(conn) -> Callable:
    """Build a fresh CheckoutHandler + probe over `conn` and return the
    top-level, timed ``process`` callable. Shared by the standalone loop and
    BackgroundTrafficDriver so the three-callable wiring lives in exactly one
    place."""
    metrics = MetricRepository(conn)
    probe = Probe.for_repo(metrics)

    handler = CheckoutHandler()
    # "Attach the wrapper to N services": the same probe.timed(...) wraps ANY
    # callable, so each layer of the checkout reports its own (service, metric)
    # and shows up as its own live-monitoring card. We wrap the two inner
    # dependencies IN PLACE so the top-level process() call drives them through
    # the wrapped versions — nested, genuinely-measured timing:
    #   payment-gateway.payment_latency_ms  — includes the retry/backoff spike
    #   fulfillment.enqueue_latency_ms       — fast; a healthy-service card
    #   checkout-service.checkout_latency_ms — the whole request (sum of both)
    handler.payments.charge = probe.timed("payment-gateway", "payment_latency_ms")(
        handler.payments.charge
    )
    handler.queue.enqueue = probe.timed("fulfillment", "enqueue_latency_ms")(
        handler.queue.enqueue
    )
    return probe.timed("checkout-service", "checkout_latency_ms")(handler.process)


def _drive(process, interval: float, stop: threading.Event) -> int:
    """The loop body: place one checkout every `interval` seconds until `stop`
    is set. Returns the number of orders processed. `stop.wait(interval)` is
    the sleep — it returns immediately once `stop` is set, so a caller (e.g.
    BackgroundTrafficDriver.stop) gets a responsive shutdown instead of waiting
    out the last interval."""
    order = 0
    while not stop.is_set():
        order += 1
        try:
            # The probe times this real call and writes checkout_latency_ms.
            process(f"order-{order}", amount=42.00)
        except CheckoutError as e:
            # A checkout that ultimately failed still emitted its (long)
            # latency sample via the probe's `finally` — keep driving.
            log.warning("checkout failed order=%s err=%s", order, e)
        stop.wait(interval)
    return order


def run(interval: float) -> None:
    conn = get_conn()
    process = _wire_probe(conn)

    log.info(
        "driving checkout-service (+ payment-gateway, fulfillment) every %.2fs "
        "(pool_size=%s, gateway degrades every %s charges)",
        interval,
        config.DB_POOL_SIZE,
        payment_gateway.GATEWAY_SLOW_EVERY,
    )
    stop = threading.Event()
    try:
        processed = _drive(process, interval, stop)
        log.info("stopping (processed %s orders)", processed)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        conn.close()


class BackgroundTrafficDriver:
    """Runs the sample-traffic loop on a daemon thread inside another process
    (the API `serve` process — see the merged web+sample task in DEPLOY.md).

    Mirrors `BackgroundWatcher` (`src/service/watcher.py`) exactly: it owns its
    OWN DB connection, separate from the API's per-request pool, and never
    crashes the web server — a failure opening the connection or a crash mid-loop
    is logged, not raised. `stop()` signals the loop's Event (so it wakes
    immediately instead of finishing its current sleep), closes the connection
    best-effort, and joins the thread for a clean task-stop on SIGTERM.
    """

    def __init__(self, interval: float = 0.5) -> None:
        self._interval = interval
        self._conn = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sample-traffic", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        """True while the daemon thread is running. False once its loop has
        exited (crash logged in `_run`, or a clean `stop()`) — mirrors
        BackgroundWatcher so `/health` can report both threads uniformly."""
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._conn = get_conn()
            process = _wire_probe(self._conn)
            log.info(
                "background traffic driver started (interval=%.2fs, gateway "
                "degrades every %s charges)",
                self._interval,
                payment_gateway.GATEWAY_SLOW_EVERY,
            )
            _drive(process, self._interval, self._stop)
        except Exception:  # noqa: BLE001 — the driver must never crash the web server
            log.exception("background traffic driver stopped (web server continues)")

    def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — teardown must not raise
                pass
        self._thread.join(timeout=5)


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
