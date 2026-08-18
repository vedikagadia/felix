"""TopologyHealthService — live-metric health correlation (Layer 3).

Given an alert, this answers "and is anything the alerting service depends on
ALSO unhealthy right now?". It:

  1. extracts which service the alert is about — the `cdc:<service>:<metric>`
     origin-node hint first, else matching a known service_nodes.name against
     the alert text (with a word-boundary guard so a stray substring can't
     false-positive);
  2. walks that service's DOWNSTREAM dependency set over the service topology
     (TopologyRepository.downstream_dependencies — the recursive-CTE walk);
  3. does ONE batched metric fetch for every dependency (MetricQueryBuilder.fetch
     — a single round trip);
  4. evaluates each dependency's configured health_checks with the matching
     MetricQueryBuilder intent, resolving each check's threshold (per-check
     override → Settings.metric_alert_thresholds → metric_alert_default_p99_ms);
  5. returns the BREACHED NodeHealths — which signal breached, observed vs.
     threshold — so the diagnoser can name the correlated failure.

Fail-safe: if no service can be extracted (or it doesn't resolve in the
topology), this returns [] and the diagnosis loop is unaffected.
"""

from __future__ import annotations

import re

import psycopg

from ..config import Settings, get_settings
from ..models import NodeHealth
from ..store.repositories import MetricRepository, TopologyRepository
from .metric_query import MetricQueryBuilder

# The four intents, mapped to the MetricQueryBuilder computation each names.
# The vocabulary is fixed (contract cross-cutting binding): a health_check with
# an unknown intent is skipped rather than guessed at.
_INTENTS = {
    "p99": MetricQueryBuilder.p99,
    "avg": MetricQueryBuilder.avg,
    "error_rate": MetricQueryBuilder.error_rate,
    "latest": MetricQueryBuilder.latest,
}

# Intents that summarize a DISTRIBUTION: a p99/avg/error_rate over a handful of
# samples is noise, so a breach on one is only trusted past a sample floor (the
# watcher's MIN_SAMPLES). `latest` is a point gauge — one sample IS its value —
# so it's exempt. Latency intents ("p99"/"avg") are the only ones a bare
# millisecond threshold default makes sense for (see _resolve_threshold).
_DISTRIBUTION_INTENTS = {"p99", "avg", "error_rate"}
_LATENCY_INTENTS = {"p99", "avg"}


class TopologyHealthService:
    def __init__(
        self,
        conn: psycopg.Connection,
        settings: Settings | None = None,
        query: MetricQueryBuilder | None = None,
        project: str = "sample",
    ) -> None:
        self.topology = TopologyRepository(conn, project)
        # MetricQueryBuilder is a thin wrapper over the metric repo; injectable
        # so a pure test can hand in a builder over a fake repo.
        self.query = query or MetricQueryBuilder(MetricRepository(conn, project))
        self.settings = settings or get_settings()

    # ── service extraction ────────────────────────────────────────────────────

    def extract_service(self, alert: str, origin_node: str | None = None) -> str | None:
        """Which service is this alert about? Prefer the CDC origin-node hint
        (`cdc:<service>:<metric>` — the watcher's synthesized alerts carry it),
        else match a known service_nodes.name against the alert text. Returns the
        resolved service name, or None so the caller can skip correlation.

        Text matching is word-boundary-guarded (a name can't match inside a
        larger token), separator-tolerant (a hyphenated name like
        "checkout-service" also matches "checkout service" / "checkout_service"
        so natural /chat and CLI phrasings resolve, not just the exact token the
        watcher synthesizes), and prefers the LONGEST known name that appears so
        a more specific service wins over a shorter substring."""
        known = self.topology.all_names()
        known_set = {n.lower() for n in known}

        # 1. CDC hint: cdc:<service>:<metric> — trust it only if the service
        # segment is actually a known node (so a malformed hint falls through).
        if origin_node and origin_node.startswith("cdc:"):
            parts = origin_node.split(":")
            if len(parts) >= 2:
                svc = parts[1]
                if svc.lower() in known_set:
                    return self._canonical(svc, known)

        # 2. Match a known name against the alert text — longest first. A name's
        # separators (-, _, space) are treated as interchangeable so
        # "checkout-service" matches free text like "checkout service is slow".
        for name in sorted(known, key=len, reverse=True):
            token = r"[\s_-]+".join(re.escape(p) for p in re.split(r"[\s_-]+", name) if p)
            pattern = r"(?<![\w-])" + token + r"(?![\w-])"
            if re.search(pattern, alert, flags=re.IGNORECASE):
                return name
        return None

    @staticmethod
    def _canonical(name: str, known: list[str]) -> str:
        """Return the known name matching `name` case-insensitively (so the CDC
        hint's casing can't diverge from the seeded node name)."""
        for n in known:
            if n.lower() == name.lower():
                return n
        return name

    # ── evaluation ─────────────────────────────────────────────────────────────

    def evaluate(
        self, alert: str, origin_node: str | None = None, max_depth: int = 3
    ) -> list[NodeHealth]:
        """The breached NodeHealths across the alerting service's downstream
        dependency set (the service itself included at depth 0). Empty when no
        service is extracted, the service doesn't resolve, or nothing breached."""
        service = self.extract_service(alert, origin_node)
        if service is None:
            return []

        deps = self.topology.downstream_dependencies(service, max_depth=max_depth)
        dep_names = [hit.node.name for hit in deps]
        if not dep_names:  # unresolved name → no dependency set
            return []

        checks_by_service = self.topology.health_checks_for(dep_names)
        # ONE batched fetch for every dependency, all metrics; bucket per metric
        # in Python (a service may carry checks on more than one metric).
        samples_by_service = self.query.fetch(dep_names)

        breached: list[NodeHealth] = []
        for svc in dep_names:
            for check in checks_by_service.get(svc, []) or []:
                nh = self._evaluate_check(svc, check, samples_by_service.get(svc, []))
                if nh is not None and nh.breached:
                    breached.append(nh)
        return breached

    def _evaluate_check(
        self, service: str, check: dict, service_samples: list[dict]
    ) -> NodeHealth | None:
        """Run one health_check ({metric,intent,threshold}) against a service's
        samples. Returns a NodeHealth, or None if the check is malformed (unknown
        intent / missing metric) or has no data to evaluate."""
        metric = check.get("metric")
        intent = check.get("intent")
        compute = _INTENTS.get(intent)
        if not isinstance(metric, str) or compute is None:
            return None

        samples = [s for s in service_samples if s.get("metric") == metric]
        observed = compute(samples)
        if observed is None:  # no data backing this signal — nothing to breach on
            return None

        threshold = self._resolve_threshold(check, intent)
        # A distribution summary (p99/avg/error_rate) over a few samples is
        # noise; only trust a breach once the window is as deep as the watcher's
        # trip rule requires (MIN_SAMPLES). A `latest` gauge is a point value —
        # one sample IS the reading — so it's exempt.
        from .watcher import MetricWatcher

        enough = intent not in _DISTRIBUTION_INTENTS or len(samples) >= MetricWatcher.MIN_SAMPLES
        return NodeHealth(
            service=service,
            metric=metric,
            intent=intent,
            observed=float(observed),
            threshold=threshold,
            breached=enough and float(observed) >= threshold,
            sample_count=len(samples),
        )

    def _resolve_threshold(self, check: dict, intent: str) -> float:
        """Per-check "threshold" wins; else Settings.metric_alert_thresholds for
        the metric; else an intent-aware default. Never re-reads env — the
        resolved Settings are the single source (contract binding).

        The default is intent-aware because metric_alert_default_p99_ms is a
        LATENCY value (~1000ms): applying it to an error_rate (a 0..1 fraction)
        would make even 100% failure (1.0 >= 1000) never breach. So a
        distribution/gauge intent that isn't latency gets no silent numeric
        default — it falls back to +inf (never breaches) unless a threshold is
        supplied explicitly, which is the safe, non-decorative behaviour."""
        t = check.get("threshold")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            return float(t)
        metric = check.get("metric")
        if metric in self.settings.metric_alert_thresholds:
            return self.settings.metric_alert_thresholds[metric]
        if intent in _LATENCY_INTENTS:
            return self.settings.metric_alert_default_p99_ms
        return float("inf")
