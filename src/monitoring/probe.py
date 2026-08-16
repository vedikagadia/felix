"""Probe — a reusable timing wrapper that feeds felix's live-monitoring panel.

Attach it to ANY function or block and it measures wall-clock latency in
milliseconds, emitting one row into the `metrics` table per call:

    probe = Probe.for_repo(MetricRepository(conn))

    @probe.timed("checkout-service", "checkout_latency_ms")   # decorator form
    def process(order_id, amount): ...

    with probe.measure("checkout-service", "checkout_latency_ms"):   # block form
        do_work()

Because the panel tails a CHANGEFEED on `metrics`, every measured call shows up
live. The probe is decoupled from the DB via an `emit` callable — trivially
testable, and it could target any sink — with `Probe.for_repo` the wiring used
in practice (write each sample through a `MetricRepository`).

Nothing here is checkout-specific: the checkout service is just the first thing
wired to it. Point `timed`/`measure` at any (service, metric) and a new card
appears on the panel automatically.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Iterator

# emit(service, metric, value_ms, labels) -> None — where a measured sample goes.
EmitFn = Callable[[str, str, float, dict], None]


class Probe:
    def __init__(self, emit: EmitFn):
        self._emit = emit

    @classmethod
    def for_repo(cls, repo) -> "Probe":
        """Build a Probe that writes each sample into the `metrics` table via a
        MetricRepository (the live path the panel reads)."""

        def emit(service: str, metric: str, value_ms: float, labels: dict) -> None:
            repo.record(service=service, metric=metric, value=value_ms, labels=labels)

        return cls(emit)

    @contextmanager
    def measure(
        self, service: str, metric: str, labels: dict | None = None
    ) -> Iterator[None]:
        """Time the wrapped block and emit its duration (ms) once it exits —
        even on exception (labelled `ok=false`), so a failing call still shows
        up on the panel rather than silently vanishing."""
        start = time.perf_counter()
        ok = True
        try:
            yield
        except Exception:
            ok = False
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._emit(service, metric, elapsed_ms, {**(labels or {}), "ok": ok})

    def timed(
        self, service: str, metric: str, labels: dict | None = None
    ) -> Callable:
        """Decorator form of `measure`: wrap a function so every call records its
        latency. `@probe.timed("checkout-service", "checkout_latency_ms")`."""

        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            def wrapper(*args, **kwargs):
                with self.measure(service, metric, labels):
                    return fn(*args, **kwargs)

            return wrapper

        return decorator
