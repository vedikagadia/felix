"""MetricWatcher — the real-time CDC anomaly loop (hop 1 of the metrics path).

Holds a sinkless CockroachDB CHANGEFEED on the `metrics` table, keeps a rolling
per-`(service, metric)` window, and when p99 latency crosses a threshold while
the average stays flat — the "dashboard still green" signature of the planted
avg-hiding-tail bug — RAISES an alert via `IncidentDiagnoser.raise_cdc_alert`.
That's LLM-free: it opens an undiagnosed cdc session (immediately visible at
/alerts). The diagnosis runs later, when an operator opens the alert (its first
/chat turn) — so detection never depends on the LLM. This class owns only window
state, the trip rule, and the dedup guard.

The changefeed is an infinite result set: it MUST be consumed via the
server-side streaming cursor (`cur.stream(...)`), never `execute` + iterate,
which buffers forever (see CDC_INTERFACE §3.2).
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from collections import deque
from math import ceil
from typing import Sequence

import psycopg

from ..store.repositories import MetricRepository
from .diagnoser import IncidentDiagnoser

log = logging.getLogger(__name__)

# Only the checkout service emits, so cold-start backfill primes this one
# window; steady-state pairs are discovered from the feed itself.
BACKFILL_SERVICE = "checkout-service"

_CHANGEFEED_SQL = "EXPERIMENTAL CHANGEFEED FOR metrics WITH updated, no_initial_scan"


class MetricWatcher:
    WINDOW = 60
    MIN_SAMPLES = 30
    P99_THRESHOLD_MS = 1000.0
    AVG_GREEN_MS = 300.0
    TARGET_METRIC = "checkout_latency_ms"

    def __init__(
        self,
        conn: psycopg.Connection,
        stream_conn: psycopg.Connection,
        metric_repo: MetricRepository,
        diagnoser: IncidentDiagnoser,
        project: str = "sample",
    ):
        # `stream_conn` holds the changefeed's server-side streaming portal and
        # does NOTHING else: psycopg forbids a second operation on a connection
        # while a stream is open, so the cooldown query and the diagnoser's
        # write-back MUST run on the separate `conn` or the stream deadlocks.
        self.conn = conn
        self.stream_conn = stream_conn
        self.metric_repo = metric_repo
        self.diagnoser = diagnoser
        # The changefeed is cluster-wide (all projects' metrics); this watcher
        # observes only its own project's rows so a pushed metric from another
        # tenant is never diagnosed against this project's memory.
        self.project = project
        self._windows: dict[tuple[str, str], deque[float]] = {}
        # origin_nodes we've already fired a diagnosis for this process — an
        # in-memory fast path over the authoritative DB cooldown (see _fire).
        self._fired: set[str] = set()

    # ── the loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Stream the changefeed forever, feeding each INSERT into the window and
        firing a diagnosis on a trip. Blocks until the caller interrupts it."""
        with self.conn.cursor() as cur:
            # Idempotent; harmless if the setting is already on (it is locally).
            # A managed Cloud cluster may forbid or pre-manage it — the feed still
            # works if rangefeed is enabled cluster-wide, so log and continue.
            try:
                cur.execute("SET CLUSTER SETTING kv.rangefeed.enabled = true")
            except psycopg.Error as exc:
                log.warning("could not set kv.rangefeed.enabled (assuming managed): %s", exc)

        self._backfill()

        log.info("watching metrics changefeed (target metric: %s)", self.TARGET_METRIC)
        with self.stream_conn.cursor() as cur:
            for row in cur.stream(_CHANGEFEED_SQL):
                # One transient hiccup — a malformed row, an LLM timeout, a DB
                # blip on write-back — must not tear down the forever-loop. Log
                # and keep consuming; the stream cursor itself stays healthy.
                try:
                    self._handle_row(row)
                except Exception:  # noqa: BLE001 — deliberately broad: keep hop-1 alive
                    log.exception("error handling changefeed row — skipping")

    def _backfill(self) -> None:
        """Prime the target-metric window from recent history so a restarted
        watcher is responsive immediately instead of waiting for MIN_SAMPLES
        fresh samples. `no_initial_scan` keeps this from double-counting: the
        feed only delivers rows inserted AFTER it starts."""
        vals = self.metric_repo.recent(BACKFILL_SERVICE, self.TARGET_METRIC, limit=self.WINDOW)
        if vals:
            self._windows[(BACKFILL_SERVICE, self.TARGET_METRIC)] = deque(
                reversed(vals), maxlen=self.WINDOW
            )
            log.info("backfilled %d samples for %s/%s", len(vals), BACKFILL_SERVICE, self.TARGET_METRIC)

    def _handle_row(self, row: tuple) -> None:
        payload = json.loads(row[2].decode())
        after = payload.get("after")
        if after is None:  # a delete — we only INSERT, so nothing to observe
            return
        if after.get("project", "sample") != self.project:  # another tenant's metric
            return
        service = after["service"]
        metric = after["metric"]
        value = float(after["value"])  # may arrive as JSON scientific notation
        log.debug("metric consumed: %s/%s = %s", service, metric, value)
        self._observe(service, metric, value)

    def _observe(self, service: str, metric: str, value: float) -> None:
        # pool_in_use etc. feed the panel narrative but don't trip (§4.2), and
        # nothing reads their window — so only the target metric is windowed.
        if metric != self.TARGET_METRIC:
            return
        window = self._windows.setdefault((service, metric), deque(maxlen=self.WINDOW))
        window.append(value)
        if self.is_anomalous(window):
            self._fire(service, metric, window)

    def _fire(self, service: str, metric: str, window: Sequence[float]) -> None:
        """Handle a trip by RAISING an alert only — NO LLM. The watcher opens an
        undiagnosed cdc session (visible at /alerts immediately) and stops there;
        the actual diagnosis runs when an operator opens the alert (its first
        /chat turn), so detection never depends on the LLM being reachable or in
        quota. Skipped entirely when an open cdc session already exists for this
        (service, metric), so a sustained spike raises exactly one alert."""
        origin_node = f"cdc:{service}:{metric}"
        # In-memory fast path: a warmed spike is anomalous on nearly every row,
        # so once we've fired for this key, short-circuit before the per-row DB
        # round-trip + log. The DB check (below) is still authoritative — it
        # survives a restart and catches a session another watcher opened.
        if origin_node in self._fired:
            return
        if self._count_open_cdc(origin_node) > 0:
            self._fired.add(origin_node)
            log.info("cooldown: open cdc session exists for %s — skipping", origin_node)
            return

        alert = self._build_alert(service, metric, window)
        log.info("TRIP: %s", alert)

        # RAISE (no LLM). Mark _fired BEFORE the write so a transient DB error
        # can't cause a re-fire storm on the next anomalous sample: raised once.
        self._fired.add(origin_node)
        session_id = self.diagnoser.raise_cdc_alert(alert, origin_node=origin_node)
        log.info("alert raised (session %s) — now visible at /alerts, diagnosis on open", session_id)

    def _count_open_cdc(self, origin_node: str) -> int:
        """Read-only dedup guard on the watcher's own connection (CDC_INTERFACE
        §4.3) — DB-backed so it survives a watcher restart."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM active_incidents
                WHERE status = 'open' AND source = 'cdc' AND origin_node = %s
                  AND project = %s
                """,
                (origin_node, self.project),
            )
            return int(cur.fetchone()[0])

    def _build_alert(self, service: str, metric: str, window: Sequence[float]) -> str:
        """Synthesized turn-0 alert — frozen wording (CDC_INTERFACE §2.3). The
        explicit "avg held flat" clause makes puzzle-B semantics recall-friendly."""
        return (
            f"p99 {metric} for {service} spiked to {self.p99(window):.0f}ms over the "
            f"last {len(window)} samples while avg held flat at "
            f"{statistics.fmean(window):.0f}ms — no dashboard alert fired."
        )

    # ── the trip rule (pure, DB-free — unit-tested by feeding a window) ───────

    @classmethod
    def is_anomalous(cls, window: Sequence[float]) -> bool:
        """The trip condition (§4.2): enough samples, a tail spike over threshold,
        and an average still in the green (the dashboard-hiding-tail signature)."""
        if len(window) < cls.MIN_SAMPLES:
            return False
        return cls.p99(window) >= cls.P99_THRESHOLD_MS and statistics.fmean(window) <= cls.AVG_GREEN_MS

    @staticmethod
    def p99(window: Sequence[float]) -> float:
        """99th percentile by the nearest-rank method (§4.1) — stdlib only, N is
        small (<= WINDOW)."""
        ordered = sorted(window)
        return ordered[min(len(ordered) - 1, ceil(0.99 * len(ordered)) - 1)]


# ── wiring: build a watcher over its own connections ──────────────────────────


def build_watcher(
    conn: psycopg.Connection, stream_conn: psycopg.Connection
) -> MetricWatcher:
    """Assemble a MetricWatcher + its diagnoser over the two supplied
    connections. Shared by the CLI (`python -m src watch`) and the in-process
    background runner so the wiring lives in exactly one place.

    `conn` runs the cooldown check + diagnoser write-back; `stream_conn` holds
    ONLY the changefeed portal (psycopg forbids a concurrent op on it) — the
    caller owns both connections' lifetimes.
    """
    from ..clients.llm import get_llm
    from ..store.repositories import (
        ActionRepository,
        ActiveIncidentRepository,
        IncidentRepository,
    )
    from .evidence_gatherer import EvidenceGatherer

    diagnoser = IncidentDiagnoser(
        EvidenceGatherer(conn),
        get_llm(),
        IncidentRepository(conn),
        ActionRepository(conn),
        ActiveIncidentRepository(conn),
    )
    return MetricWatcher(conn, stream_conn, MetricRepository(conn), diagnoser)


class BackgroundWatcher:
    """Runs a MetricWatcher on a daemon thread inside another process (the API
    `serve` process — see the merged web+watch task in DEPLOY.md §4).

    It owns its OWN two DB connections, separate from the API's per-request
    pool: the changefeed is a long-lived server-side portal that must not touch
    a request-scoped connection. The embedding model, however, IS shared with
    the API — `get_embedder()` is a process singleton — which is the whole point
    of merging (one ~4GB task instead of two).

    Best-effort and self-contained: a failure opening connections or a crash in
    the stream loop is logged, not raised, so it can never take the web server
    down. `stop()` closes the changefeed and both connections for a clean
    task-stop on SIGTERM.
    """

    def __init__(self) -> None:
        self._conn: psycopg.Connection | None = None
        self._stream_conn: psycopg.Connection | None = None
        self._thread = threading.Thread(
            target=self._run, name="cdc-watcher", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        """True while the daemon thread is running. False once its loop has
        exited (crash logged in `_run`, or a clean `stop()`) — this is what
        `/health` reports so a silently-dead watcher is observable."""
        return self._thread.is_alive()

    def _run(self) -> None:
        from ..store.connection import get_conn

        try:
            self._conn = get_conn()
            self._stream_conn = get_conn()
            build_watcher(self._conn, self._stream_conn).run()
        except Exception:  # noqa: BLE001 — the watcher must never crash the web server
            log.exception("background watcher stopped (web server continues)")

    def stop(self) -> None:
        # Closing stream_conn unblocks the `cur.stream(...)` loop; both closes
        # are best-effort so shutdown proceeds even if one already died.
        for c in (self._stream_conn, self._conn):
            if c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001 — teardown must not raise
                    pass
        self._thread.join(timeout=5)
