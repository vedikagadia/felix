"""MetricQueryBuilder — the live-metric query primitive (Layer 1).

Pure intents over an in-memory list of metric samples (the dicts
MetricRepository.recent_samples / .recent_by_services return), plus one batched
fetch that pulls many services in a single round trip. The compute intents own
no DB access; the ONLY DB call is `fetch`, which delegates to the repository.

p99/avg are NOT re-derived here: p99 delegates to MetricWatcher.p99 (nearest-rank)
and avg to statistics.fmean — the canonical trip math, so a live query and the
watcher can never disagree on what a percentile means.
"""

from __future__ import annotations

import statistics

from ..store.repositories import MetricRepository


class MetricQueryBuilder:
    def __init__(self, repo: MetricRepository) -> None:
        self.repo = repo

    # ── pure intents over an in-memory sample list (no DB) ────────────────────

    @staticmethod
    def p99(samples: list[dict]) -> float | None:
        """99th percentile of the samples' values via MetricWatcher.p99
        (nearest-rank); None if `samples` is empty.

        MetricWatcher is imported lazily here (not at module scope) purely to
        break an import cycle: watcher.py imports the diagnoser, which reaches
        the evidence gatherer → this module. The delegation itself is unchanged —
        p99 is still the watcher's canonical nearest-rank math, never re-derived,
        so a live query and the watcher's trip rule can't disagree."""
        if not samples:
            return None
        from .watcher import MetricWatcher

        return MetricWatcher.p99([s["value"] for s in samples])

    @staticmethod
    def avg(samples: list[dict]) -> float | None:
        """Mean of the samples' values via statistics.fmean; None if empty."""
        if not samples:
            return None
        return statistics.fmean(s["value"] for s in samples)

    @staticmethod
    def error_rate(samples: list[dict]) -> float | None:
        """Fraction of samples whose labels.ok is False, in [0.0, 1.0]; None if
        empty. A missing/None labels dict (or a labels dict without "ok") counts
        as ok=True — matching how the probe labels a successful call."""
        if not samples:
            return None
        failed = 0
        for s in samples:
            labels = s.get("labels") or {}
            if labels.get("ok", True) is False:
                failed += 1
        return failed / len(samples)

    @staticmethod
    def latest(samples: list[dict]) -> float | None:
        """Value of the newest sample (rows are newest-first); None if empty."""
        if not samples:
            return None
        return samples[0]["value"]

    # ── the single batched fetch: one round trip for many services ────────────

    def fetch(
        self, services: list[str], metric: str | None = None, limit: int = 200
    ) -> dict[str, list[dict]]:
        """Recent samples for many services in ONE query (service = ANY(%s) via
        MetricRepository.recent_by_services), grouped by service (newest-first
        within each). Every requested service is present in the result; ones
        with no rows map to []."""
        grouped: dict[str, list[dict]] = {svc: [] for svc in services}
        for row in self.repo.recent_by_services(services, metric=metric, limit=limit):
            grouped.setdefault(row["service"], []).append(row)
        return grouped
