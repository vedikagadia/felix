"""Deterministic tests for the live-metric querying feature — no network, and
DB-free except the one traversal test that skips when the local node is down.

Covers:
  - the pure MetricQueryBuilder intents (p99 delegates to MetricWatcher.p99, avg,
    error_rate, latest) and their empty-input contract;
  - MetricQueryBuilder.fetch's bucketing (every requested service present);
  - TopologyHealthService breach detection + threshold fallback + fail-safe;
  - service extraction (CDC hint, text hit, miss, false-positive guard);
  - RunbookRepository / TopologyRepository result shaping over injected rows;
  - the topology traversal against the local DB (skipped if unreachable).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models import GraphHit, CodeNode, NodeHealth, Recall, Runbook, RunbookStep
from src.service.metric_query import MetricQueryBuilder
from src.service.topology_health import TopologyHealthService
from src.service.watcher import MetricWatcher


# ── MetricQueryBuilder: pure intents ─────────────────────────────────────────


def _samples(values, ok=None, metric="m"):
    """Build recent_samples-shaped dicts (newest-first as the caller passes them)."""
    out = []
    for v in values:
        labels = None if ok is None else {"ok": ok}
        out.append(
            {"service": "s", "metric": metric, "value": float(v), "ts": None, "labels": labels}
        )
    return out


def _window(peak, baseline, metric="m", n=None):
    """A distribution-intent window deep enough to clear the min-sample floor
    (MetricWatcher.MIN_SAMPLES): one `peak` sample among `baseline` filler, so
    nearest-rank p99 lands on `peak`. `n` defaults to the floor size."""
    n = n or MetricWatcher.MIN_SAMPLES
    return _samples([peak] + [baseline] * (n - 1), metric=metric)


def test_p99_delegates_to_watcher():
    vals = list(range(1, 101))
    samples = _samples(vals)
    # Same nearest-rank math as the watcher — must be identical, not merely close.
    assert MetricQueryBuilder.p99(samples) == MetricWatcher.p99([float(v) for v in vals])


def test_avg_is_fmean():
    samples = _samples([10, 20, 30])
    assert MetricQueryBuilder.avg(samples) == pytest.approx(20.0)


def test_error_rate_counts_ok_false():
    samples = _samples([1, 2, 3, 4], ok=None)  # missing labels → ok=True
    samples[0]["labels"] = {"ok": False}
    samples[1]["labels"] = {"ok": False}
    samples[2]["labels"] = {}  # no "ok" key → ok=True
    samples[3]["labels"] = None  # None labels → ok=True
    assert MetricQueryBuilder.error_rate(samples) == pytest.approx(0.5)


def test_error_rate_all_ok_is_zero():
    assert MetricQueryBuilder.error_rate(_samples([1, 2, 3], ok=True)) == 0.0


def test_latest_is_newest_first():
    # recent_samples returns newest-first, so latest == samples[0].
    assert MetricQueryBuilder.latest(_samples([99, 5, 5])) == 99.0


@pytest.mark.parametrize("intent", ["p99", "avg", "error_rate", "latest"])
def test_intents_none_on_empty(intent):
    fn = getattr(MetricQueryBuilder, intent)
    assert fn([]) is None


# ── MetricQueryBuilder.fetch: bucketing over a fake repo ─────────────────────


class _FakeMetricRepo:
    """Stands in for MetricRepository.recent_by_services — records the call and
    returns canned rows so fetch's grouping can be asserted DB-free."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def recent_by_services(self, services, metric=None, limit=200):
        self.calls.append((list(services), metric, limit))
        return self._rows


def test_fetch_buckets_and_includes_every_service():
    rows = [
        {"service": "a", "metric": "m", "value": 1.0, "ts": None, "labels": None},
        {"service": "a", "metric": "m", "value": 2.0, "ts": None, "labels": None},
    ]
    qb = MetricQueryBuilder(_FakeMetricRepo(rows))
    grouped = qb.fetch(["a", "b"], metric="m")
    assert [s["value"] for s in grouped["a"]] == [1.0, 2.0]
    assert grouped["b"] == []  # requested but no rows → present as []
    # ONE round trip, the metric narrowed through.
    assert qb.repo.calls == [(["a", "b"], "m", 200)]


# ── TopologyHealthService: extraction, breach, threshold fallback ────────────


class _FakeTopo:
    def __init__(self, names, deps, checks):
        self._names = names
        self._deps = deps  # service -> list[GraphHit]
        self._checks = checks  # name -> list[dict]

    def all_names(self):
        return self._names

    def downstream_dependencies(self, service, max_depth=3):
        return self._deps.get(service, [])

    def health_checks_for(self, services):
        return {s: self._checks.get(s, []) for s in services if s in self._checks}


class _FakeQuery:
    def __init__(self, samples_by_service):
        self._by = samples_by_service

    def fetch(self, services, metric=None, limit=200):
        return {s: self._by.get(s, []) for s in services}


def _hit(name):
    return GraphHit(node=CodeNode(id=name, name=name, kind="service", service=name), depth=1)


def _service(names=None, deps=None, checks=None, samples=None, settings=None):
    """Build a TopologyHealthService with injected fakes (no DB, no env)."""
    svc = TopologyHealthService.__new__(TopologyHealthService)
    svc.topology = _FakeTopo(names or [], deps or {}, checks or {})
    svc.query = _FakeQuery(samples or {})
    svc.settings = settings or SimpleNamespace(
        metric_alert_thresholds={}, metric_alert_default_p99_ms=1000.0
    )
    return svc


def test_extract_service_from_cdc_hint():
    svc = _service(names=["checkout-service", "payment-gateway"])
    got = svc.extract_service(
        "p99 spiked", origin_node="cdc:checkout-service:checkout_latency_ms"
    )
    assert got == "checkout-service"


def test_extract_service_from_alert_text():
    svc = _service(names=["checkout-service", "payment-gateway"])
    assert svc.extract_service("the payment-gateway is slow") == "payment-gateway"


def test_extract_service_miss_returns_none():
    svc = _service(names=["checkout-service"])
    assert svc.extract_service("some unrelated database alert") is None


def test_extract_service_false_positive_guard():
    # "fulfillment" must NOT match inside "prefulfillmentx" — word-boundary guard.
    svc = _service(names=["fulfillment"])
    assert svc.extract_service("prefulfillmentx exploded") is None
    assert svc.extract_service("the fulfillment queue backed up") == "fulfillment"


def test_extract_service_prefers_longest_name():
    svc = _service(names=["payment", "payment-gateway"])
    # both could appear, but the more specific (longer) name wins.
    assert svc.extract_service("payment-gateway degraded") == "payment-gateway"


def test_evaluate_returns_only_breached():
    checks = {
        "checkout-service": [{"metric": "checkout_latency_ms", "intent": "p99", "threshold": 1000}],
        "payment-gateway": [{"metric": "payment_latency_ms", "intent": "p99", "threshold": 800}],
    }
    deps = {
        "checkout-service": [_hit("checkout-service"), _hit("payment-gateway")],
    }
    samples = {
        "checkout-service": _window(120, 110, metric="checkout_latency_ms"),  # p99 ~120 < 1000 → healthy
        "payment-gateway": _window(1200, 100, metric="payment_latency_ms"),  # p99 1200 >= 800 → breached
    }
    svc = _service(
        names=["checkout-service", "payment-gateway"], deps=deps, checks=checks, samples=samples
    )
    breached = svc.evaluate("checkout-service is slow", origin_node="cdc:checkout-service:checkout_latency_ms")
    assert len(breached) == 1
    nh = breached[0]
    assert isinstance(nh, NodeHealth)
    assert nh.service == "payment-gateway"
    assert nh.intent == "p99"
    assert nh.breached is True
    assert nh.observed >= nh.threshold


def test_evaluate_fail_safe_no_service():
    svc = _service(names=["checkout-service"])
    assert svc.evaluate("nothing recognizable here") == []


def test_evaluate_skips_checks_with_no_data():
    checks = {"payment-gateway": [{"metric": "payment_latency_ms", "intent": "p99", "threshold": 1}]}
    deps = {"checkout-service": [_hit("payment-gateway")]}
    svc = _service(
        names=["checkout-service", "payment-gateway"],
        deps=deps,
        checks=checks,
        samples={"payment-gateway": []},  # no samples → no NodeHealth
    )
    assert svc.evaluate("checkout-service slow") == []


def test_threshold_fallback_chain():
    settings = SimpleNamespace(
        metric_alert_thresholds={"payment_latency_ms": 800.0},
        metric_alert_default_p99_ms=1000.0,
    )
    svc = _service(settings=settings)
    # 1. per-check override wins (any intent)
    assert svc._resolve_threshold({"metric": "m", "threshold": 42}, "p99") == 42.0
    # 2. else Settings per-metric threshold (any intent)
    assert svc._resolve_threshold({"metric": "payment_latency_ms"}, "p99") == 800.0
    # 3. else, for a LATENCY intent, the global ms default
    assert svc._resolve_threshold({"metric": "unknown_metric"}, "p99") == 1000.0


def test_threshold_default_is_intent_aware():
    # The ms default (1000) must NOT be applied to a non-latency intent: an
    # error_rate is a 0..1 fraction, so defaulting to 1000 would make even 100%
    # failure never breach. With no explicit/config threshold it resolves to
    # +inf (never breaches) instead of a nonsensical millisecond value.
    settings = SimpleNamespace(metric_alert_thresholds={}, metric_alert_default_p99_ms=1000.0)
    svc = _service(settings=settings)
    assert svc._resolve_threshold({"metric": "err"}, "error_rate") == float("inf")
    assert svc._resolve_threshold({"metric": "gauge"}, "latest") == float("inf")
    # but a supplied threshold is still honoured for those intents
    assert svc._resolve_threshold({"metric": "err", "threshold": 0.05}, "error_rate") == 0.05


def test_breach_needs_min_samples_for_distribution_intents():
    # A distribution intent (p99) breaching on a handful of samples is noise:
    # below MetricWatcher.MIN_SAMPLES the check is computed but NOT breached.
    checks = {"payment-gateway": [{"metric": "payment_latency_ms", "intent": "p99", "threshold": 800}]}
    deps = {"checkout-service": [_hit("payment-gateway")]}
    names = ["checkout-service", "payment-gateway"]

    thin = {"payment-gateway": _samples([100, 100, 1200], metric="payment_latency_ms")}  # 3 samples
    svc_thin = _service(names=names, deps=deps, checks=checks, samples=thin)
    assert svc_thin.evaluate("checkout-service slow") == []  # under floor → not breached

    deep = {"payment-gateway": _window(1200, 100, metric="payment_latency_ms")}  # >= floor
    svc_deep = _service(names=names, deps=deps, checks=checks, samples=deep)
    assert len(svc_deep.evaluate("checkout-service slow")) == 1  # floor cleared → breached


def test_latest_gauge_breaches_on_one_sample():
    # `latest` is a point gauge, not a distribution — exempt from the floor.
    checks = {"connection-pool": [{"metric": "pool_in_use", "intent": "latest", "threshold": 95}]}
    deps = {"checkout-service": [_hit("connection-pool")]}
    samples = {"connection-pool": _samples([98], metric="pool_in_use")}  # single reading
    svc = _service(
        names=["checkout-service", "connection-pool"], deps=deps, checks=checks, samples=samples
    )
    breached = svc.evaluate("checkout-service slow")
    assert len(breached) == 1
    assert breached[0].intent == "latest"


def test_evaluate_uses_threshold_fallback_when_check_omits_it():
    # check has NO threshold → resolves to the per-metric Settings value (800).
    settings = SimpleNamespace(
        metric_alert_thresholds={"payment_latency_ms": 800.0}, metric_alert_default_p99_ms=1000.0
    )
    checks = {"payment-gateway": [{"metric": "payment_latency_ms", "intent": "p99"}]}
    deps = {"checkout-service": [_hit("payment-gateway")]}
    samples = {"payment-gateway": _window(900, 100, metric="payment_latency_ms")}  # p99 900 >= 800
    svc = _service(
        names=["checkout-service", "payment-gateway"],
        deps=deps,
        checks=checks,
        samples=samples,
        settings=settings,
    )
    breached = svc.evaluate("checkout-service slow")
    assert len(breached) == 1
    assert breached[0].threshold == 800.0


# ── recall / result shaping ───────────────────────────────────────────────────


def test_runbook_recall_shape_over_fake_rows():
    """RunbookRepository.recall + _attach_steps shaping, exercised without the DB
    by driving the two cursor round-trips through a fake connection."""
    from src.store.repositories import RunbookRepository

    recall_rows = [
        ("rb-1", "title one", "symptoms", "svc", ["t1", "t2"], None, 0.12),
    ]
    step_rows = [
        ("rb-1", 1, "do a", "cmd a", "ok a"),
        ("rb-1", 2, "do b", None, None),
    ]

    class _Cur:
        def __init__(self):
            self._q = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            # first execute = recall SELECT, second = _attach_steps SELECT
            self._which = "steps" if "runbook_steps" in sql else "recall"

        def fetchall(self):
            return step_rows if self._which == "steps" else recall_rows

    class _Conn:
        def cursor(self):
            return _Cur()

    repo = RunbookRepository(_Conn())
    out = repo.recall([0.0] * 1024, k=5)
    assert len(out) == 1
    rc = out[0]
    assert isinstance(rc, Recall)
    assert isinstance(rc.item, Runbook)
    assert rc.item.id == "rb-1"
    assert rc.item.tags == ["t1", "t2"]
    assert rc.distance == pytest.approx(0.12)
    assert [s.step_order for s in rc.item.steps] == [1, 2]
    assert rc.item.steps[0].command == "cmd a"
    assert isinstance(rc.item.steps[0], RunbookStep)


# ── topology traversal against the local DB (skips if unreachable) ────────────


@pytest.fixture
def conn():
    from src.store.connection import get_conn

    try:
        c = get_conn()
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"local CockroachDB not reachable: {e}")
    c.autocommit = False
    tx = c.transaction()
    tx.__enter__()
    try:
        yield c
    finally:
        tx.__exit__(RuntimeError, RuntimeError("rollback test txn"), None)
        c.close()


def test_downstream_dependencies_over_local_db(conn):
    """Seed a tiny topology in the test's rolled-back transaction and walk it."""
    from src.store.repositories import TopologyRepository

    repo = TopologyRepository(conn)
    repo.upsert_node(id="11111111-0000-0000-0000-000000000001", name="tsvc-a", kind="service", summary=None, health_checks=[{"metric": "m", "intent": "p99", "threshold": 5}])
    repo.upsert_node(id="11111111-0000-0000-0000-000000000002", name="tsvc-b", kind="service", summary=None, health_checks=[])
    repo.upsert_node(id="11111111-0000-0000-0000-000000000003", name="tsvc-c", kind="service", summary=None, health_checks=[])
    repo.upsert_edge(src_id="11111111-0000-0000-0000-000000000001", dst_id="11111111-0000-0000-0000-000000000002", kind="depends_on")
    repo.upsert_edge(src_id="11111111-0000-0000-0000-000000000002", dst_id="11111111-0000-0000-0000-000000000003", kind="depends_on")

    hits = repo.downstream_dependencies("tsvc-a", max_depth=3)
    by_name = {h.node.name: h.depth for h in hits}
    assert by_name == {"tsvc-a": 0, "tsvc-b": 1, "tsvc-c": 2}

    # unresolved name degrades to []
    assert repo.downstream_dependencies("does-not-exist") == []

    # health_checks_for reads the JSONB back already-decoded (list[dict])
    checks = repo.health_checks_for(["tsvc-a", "tsvc-b"])
    assert checks["tsvc-a"] == [{"metric": "m", "intent": "p99", "threshold": 5}]
    assert checks["tsvc-b"] == []
