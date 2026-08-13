"""Pure-logic tests for MetricWatcher's anomaly rule + p99 — no DB, no network.

The trip rule is a classmethod fed a window directly, so these assert the three
cases the demo hinges on: a tail spike trips, a flat/normal window doesn't, and
a window whose average is NOT green doesn't (even with a big tail) — the
"dashboard still green" clause. p99 is checked against a hand-computable window.
"""

from __future__ import annotations

from collections import deque

from src.service.watcher import MetricWatcher

W = MetricWatcher


def _window(baseline: float, spikes: list[float], n: int = W.WINDOW) -> deque[float]:
    """A window of `n` values: mostly `baseline`, with `spikes` appended (the
    tail). Fills to WINDOW so len >= MIN_SAMPLES."""
    vals = [baseline] * (n - len(spikes)) + spikes
    return deque(vals, maxlen=W.WINDOW)


def test_spike_trips():
    # 59 low samples + one 2500ms tail: p99 >= 1000, avg well under 300.
    window = _window(90.0, [2500.0])
    assert W.is_anomalous(window)


def test_normal_window_does_not_trip():
    window = _window(95.0, [])
    assert not W.is_anomalous(window)


def test_below_min_samples_does_not_trip():
    window = _window(90.0, [2500.0], n=W.MIN_SAMPLES - 1)
    assert not W.is_anomalous(window)


def test_avg_not_green_does_not_trip():
    # A genuine broad slowdown: high p99 but the average is also elevated, so
    # the dashboard would already be red — not our hidden-tail signature.
    window = deque([1500.0] * W.WINDOW, maxlen=W.WINDOW)
    assert W.p99(window) >= W.P99_THRESHOLD_MS
    assert not W.is_anomalous(window)


def test_p99_nearest_rank():
    # 100 values 1..100: nearest-rank 99th percentile = ceil(0.99*100)-1 = 98th
    # index of the sorted list = value 99.
    window = deque([float(i) for i in range(1, 101)], maxlen=1000)
    assert W.p99(window) == 99.0


def test_p99_small_window():
    window = deque([float(i) for i in range(1, 31)], maxlen=W.WINDOW)  # 1..30
    # ceil(0.99*30)-1 = ceil(29.7)-1 = 30-1 = 29 -> the max, value 30.
    assert W.p99(window) == 30.0
