"""Schema-team tests — MetricRepository round-trip + the source='chat' default.

Both hit the local CockroachDB and skip if it's unreachable, mirroring
test_writeback.py: each runs inside an OUTER transaction rolled back in teardown
so the seeded DB is left untouched.
"""

from __future__ import annotations

import pytest

from src.store.connection import get_conn
from src.store.repositories import (
    ActiveIncidentRepository,
    IncidentRepository,
    MetricRepository,
)


@pytest.fixture
def conn():
    try:
        c = get_conn()
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:  # noqa: BLE001 - any connect/probe failure => skip
        pytest.skip(f"local CockroachDB not reachable: {e}")
    c.autocommit = False
    tx = c.transaction()
    tx.__enter__()
    try:
        yield c
    finally:
        tx.__exit__(RuntimeError, RuntimeError("rollback test txn"), None)
        c.close()


def test_metric_record_then_recent_roundtrip(conn):
    repo = MetricRepository(conn)
    svc, metric = "checkout-service", "test_metric_roundtrip"
    repo.record(service=svc, metric=metric, value=100.0)
    repo.record(service=svc, metric=metric, value=250.5, labels={"attempt": 3})

    values = repo.recent(svc, metric, limit=10)
    # Bare floats, both samples round-tripped. (Both inserts share this test's
    # transaction, so now() gives them the same ts — the newest-first ordering
    # can't be tie-broken here, so assert the set, not the order.)
    assert sorted(values) == [100.0, 250.5]


def test_create_session_defaults_source_to_chat(conn):
    """Existing create_session callers omit source — they must still get 'chat',
    proving the migration didn't break the chat path."""
    repo = ActiveIncidentRepository(conn)
    session_id = repo.create_session(alert="checkout failing")
    assert repo.get_session(session_id).source == "chat"


def test_create_session_cdc_source_is_queryable(conn):
    """A cdc session is found by list_alerts + count_open (the watcher/API path)."""
    repo = ActiveIncidentRepository(conn)
    origin = "cdc:checkout-service:checkout_latency_ms"
    session_id = repo.create_session(alert="p99 spiked", origin_node=origin, source="cdc")

    assert repo.count_open("cdc", origin) == 1
    alerts = repo.list_alerts(source="cdc", status="open")
    assert any(a["id"] == session_id and a["origin_node"] == origin for a in alerts)


def test_insert_minimal_persists_root_cause(conn):
    """insert_minimal must store root_cause so get() (hence GET /sessions) can
    reconstruct a CDC diagnosis — for a CDC alert this is the ONLY channel that
    delivers felix's root cause to the UI, so a NULL here blanks the card."""
    repo = IncidentRepository(conn)
    incident_id = repo.insert_minimal(
        title="t", symptoms="s", root_cause="held connection across retry loop", service="checkout"
    )
    got = repo.get(incident_id)
    assert got is not None
    assert got.root_cause == "held connection across retry loop"
