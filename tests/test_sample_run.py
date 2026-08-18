"""Pure-logic tests for the sample-traffic driver's loop body — no DB, no
thread, no network.

`_drive` is a plain function fed a fake `process` callable and a
`threading.Event`, mirroring how BackgroundTrafficDriver and the standalone
`run()` both use it — so these pin the stop-signal and CheckoutError-survival
behavior directly, the same way test_watcher.py pins MetricWatcher's trip rule.
"""

from __future__ import annotations

import threading

# Import `run` first: it does the sys.path insert that makes `checkout_service`
# importable by bare name (see its module docstring) — `checkout.py` needs that
# to resolve its own `from checkout_service import db`.
from sample_project.run import _drive
from checkout_service.checkout import CheckoutError


def test_drive_stops_when_event_is_set():
    """Each call sets `stop` after the 3rd order, so the loop must not place a
    4th — proves `stop.wait(interval)` (with interval=0) is checked every
    iteration, not just at the top."""
    stop = threading.Event()
    calls = []

    def process(order_id, amount):
        calls.append(order_id)
        if len(calls) == 3:
            stop.set()

    _drive(process, interval=0, stop=stop)

    assert calls == ["order-1", "order-2", "order-3"]


def test_drive_survives_checkout_error_and_keeps_going():
    """A CheckoutError on one order must not kill the loop — the failing call
    still counts toward the order sequence, and the next order is attempted."""
    stop = threading.Event()
    calls = []

    def process(order_id, amount):
        calls.append(order_id)
        if len(calls) == 2:
            raise CheckoutError("boom")
        if len(calls) == 3:
            stop.set()

    _drive(process, interval=0, stop=stop)

    assert calls == ["order-1", "order-2", "order-3"]


def test_drive_does_nothing_if_already_stopped():
    """A pre-set stop Event means zero orders — no off-by-one at the boundary."""
    stop = threading.Event()
    stop.set()
    calls = []

    _drive(lambda order_id, amount: calls.append(order_id), interval=0, stop=stop)

    assert calls == []
