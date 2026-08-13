"""Write-back tests — FakeLLM + the local CockroachDB.

These exercise IncidentDiagnoser.diagnose()'s side effects: the atomic
insert-incident + resolution_steps + audit-row sequence, and that a forced
failure leaves NO orphan rows (the transaction fix).

Isolation: each test runs inside an OUTER transaction that is rolled back in
teardown, so nothing it writes survives — the seeded DB is left untouched. (Our
production write-back opens its own inner transaction via
`conn.transaction()`; psycopg turns a nested one into a SAVEPOINT, so the outer
rollback still discards everything.)

Skipped automatically if DATABASE_URL isn't reachable, so the deterministic
suite (test_parsing.py) still runs anywhere.
"""

from __future__ import annotations

import json

import pytest

from src.models import Incident, Recall
from src.store.connection import get_conn
from src.store.repositories import ActionRepository, IncidentRepository


@pytest.fixture
def conn():
    try:
        c = get_conn()
        # cheap connectivity probe
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:  # noqa: BLE001 - any connect/probe failure => skip
        pytest.skip(f"local CockroachDB not reachable: {e}")
    # Wrap the whole test in a transaction we roll back, so writes don't persist.
    # autocommit is on by default; disable it so the outer block is a real txn.
    c.autocommit = False
    tx = c.transaction()
    tx.__enter__()
    try:
        yield c
    finally:
        # Force rollback regardless of what the test did.
        tx.__exit__(RuntimeError, RuntimeError("rollback test txn"), None)
        c.close()


def _diagnoser(conn, llm):
    """Build an IncidentDiagnoser wired to a stub gatherer that returns a fixed
    packet — we're testing WRITE-BACK, not retrieval, so we don't hit the
    embedder. Only .diagnose's write path and _resolve_origin need the gatherer;
    we pass a packet with no upstream so origin resolution is skipped."""
    from src.models import EvidencePacket

    class _StubGatherer:
        class graph:  # unused here; present so attribute access wouldn't crash
            pass

        def gather(self, alert, origin_node=None, k=3):
            # a close incident so _infer_service can read it, but no code tokens
            return EvidencePacket(
                alert=alert,
                incidents=[Recall(item=Incident(id="seed-x", title="t", symptoms="s", service="checkout"), distance=0.9)],
            )

    from src.service.diagnoser import IncidentDiagnoser

    return IncidentDiagnoser(
        gatherer=_StubGatherer(),
        llm=llm,
        incident_repo=IncidentRepository(conn),
        action_repo=ActionRepository(conn),
    )


def _counts(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM incidents WHERE embedding IS NULL),"
            "       (SELECT count(*) FROM resolution_steps),"
            "       (SELECT count(*) FROM agent_actions)"
        )
        return cur.fetchone()


def test_diagnose_writes_exactly_one_of_each(conn):
    from tests.conftest import FakeLLM

    payload = {
        "summary": "pool exhausted",
        "root_cause": "held connection",
        "proposed_steps": [
            {"action": "increase pool", "command": None, "outcome": "headroom"},
            {"action": "refactor handler", "command": None, "outcome": "fix"},
        ],
        "cited_incident_ids": [],
        "cited_change_ids": [],
        "confidence": 0.9,
    }
    llm = FakeLLM(json.dumps(payload))
    diagnoser = _diagnoser(conn, llm)

    before = _counts(conn)
    diagnosis = diagnoser.diagnose("checkout db.pool.exhausted")
    after = _counts(conn)

    assert after[0] == before[0] + 1, "exactly one null-embedding incident"
    assert after[1] == before[1] + 2, "two resolution steps (matching proposed_steps)"
    assert after[2] == before[2] + 1, "exactly one audit row"
    assert diagnosis.incident_id is not None


def test_audit_row_is_valid_json(conn):
    from tests.conftest import FakeLLM

    llm = FakeLLM(json.dumps({"summary": "s", "proposed_steps": [], "confidence": 0.5}))
    diagnoser = _diagnoser(conn, llm)
    diagnoser.diagnose("some alert text")

    with conn.cursor() as cur:
        cur.execute("SELECT action_type, tool_called, input, output, model FROM agent_actions ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    action_type, tool_called, input_col, output_col, model = row
    assert action_type == "diagnose"
    assert tool_called == "respond"
    assert model == "fake-model"
    # psycopg returns JSONB as already-parsed Python objects. session_id is None
    # here because this diagnoser is wired without an ActiveIncidentRepository
    # (single-turn); it records which conversation a diagnosis belongs to.
    assert input_col == {"alert": "some alert text", "session_id": None}
    assert output_col["summary"] == "s"


def test_failed_writeback_leaves_no_orphan(conn):
    """If a step insert fails mid-sequence, the whole write-back must roll back
    — no incident row without its steps/audit. We force the failure by making
    add_resolution_steps raise, then assert counts are unchanged."""
    from tests.conftest import FakeLLM

    payload = {"summary": "s", "proposed_steps": [{"action": "a"}], "confidence": 0.5}
    llm = FakeLLM(json.dumps(payload))
    diagnoser = _diagnoser(conn, llm)

    # Sabotage the step insert to raise AFTER the incident insert has run.
    def _boom(*a, **k):
        raise RuntimeError("simulated DB failure on step insert")

    diagnoser.incident_repo.add_resolution_steps = _boom

    before = _counts(conn)
    with pytest.raises(RuntimeError):
        diagnoser.diagnose("checkout db.pool.exhausted")
    after = _counts(conn)

    assert after == before, "transaction rolled back: no orphan incident/steps/audit"
