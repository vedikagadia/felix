"""Diagnosis-QUALITY tests — the two planted puzzles, against the LIVE model.

A FakeLLM cannot verify quality (it would just echo whatever we scripted), so
these hit the real LLM + real DB and assert FUZZY invariants that tolerate
wording variation: which memory source the diagnosis leans on, and whether it
cites the right ids — not exact strings.

Marked `live` and skipped by default. Run explicitly with:
    ./.venv/bin/python -m pytest -m live

These double as a visible demo: each test prints the alert, the evidence packet
it recalled (incidents / docs / changes / trace), and the diagnosis. pytest
CAPTURES stdout and only shows it when a test FAILS — so to watch a PASSING run,
add -s:
    ./.venv/bin/python -m pytest -m live -s

Requires: local CockroachDB seeded + GEMINI_API_KEY set (or LLM_PROVIDER
pointing at a reachable provider). Each run writes one incident; the module
cleans up its null-embedding rows in teardown.
"""

from __future__ import annotations

import pytest

from src.config import get_settings

pytestmark = pytest.mark.live


def _run_and_show(diagnoser, alert: str, origin_node: str | None = None):
    """Gather + print the evidence packet, diagnose + print the result, then
    return the Diagnosis. Reuses the CLI's formatters so the demo output matches
    `python -m src respond`. (The packet is gathered here for display; diagnose()
    re-gathers internally — a negligible second embed for a 3-test demo.)"""
    from src.cli import _print_diagnosis, _print_packet

    packet = diagnoser.gatherer.gather(alert, origin_node=origin_node)
    _print_packet(packet)
    diagnosis = diagnoser.diagnose(alert, origin_node=origin_node)
    _print_diagnosis(diagnosis)
    return diagnosis


@pytest.fixture(scope="module")
def diagnoser():
    settings = get_settings()
    if settings.llm_provider == "gemini" and not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY not set — live puzzle tests need a real LLM")

    try:
        from src.clients.llm import get_llm
        from src.service.diagnoser import IncidentDiagnoser
        from src.service.evidence_gatherer import EvidenceGatherer
        from src.store.connection import get_conn
        from src.store.repositories import ActionRepository, IncidentRepository

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"live deps unavailable: {e}")

    gatherer = EvidenceGatherer(conn)
    d = IncidentDiagnoser(gatherer, get_llm(), IncidentRepository(conn), ActionRepository(conn))
    yield d
    # clean up any incident rows these tests wrote (null embedding = live-diagnosed)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM incidents WHERE embedding IS NULL")
    conn.close()


def test_puzzle_a_code_only(diagnoser):
    """Symptom looks like a DB capacity issue; the true cause is only visible
    from the code graph (connection held across the payment retry loop)."""
    d = _run_and_show(
        diagnoser,
        "checkout requests failing during flash sale, db.pool.exhausted, database capacity looks fine",
        origin_node="ConnectionPool.acquire",
    )
    assert d.root_cause is not None
    rc = d.root_cause.lower()
    assert any(k in rc for k in ("connection", "checkouthandler", "payment", "pool")), rc
    # No code change is relevant to puzzle A.
    assert d.cited_change_ids == [], d.cited_change_ids


def test_puzzle_b_merge_only(diagnoser):
    """Dashboards green, no alert — solvable only via the recent code change
    that switched LATENCY_AGGREGATION p99 -> avg. No origin_node given."""
    d = _run_and_show(
        diagnoser,
        "customers say checkout is slow but the latency dashboard is green and no alert fired",
    )
    assert d.root_cause is not None
    rc = d.root_cause.lower()
    assert any(k in rc for k in ("avg", "p99", "aggregation", "metric")), rc
    # Must cite the code change that reveals it.
    assert d.cited_change_ids, "puzzle B should cite the p99->avg code change"


def test_negative_control_no_hallucination(diagnoser):
    """An unrelated alert must NOT produce a confident checkout root cause or
    cite unrelated ids."""
    d = _run_and_show(diagnoser, "the office wifi is down")
    # Either no root cause, or at least no fabricated checkout citations.
    assert not d.cited_change_ids
    if d.root_cause is not None:
        rc = d.root_cause.lower()
        assert not any(k in rc for k in ("checkouthandler", "db.pool", "latency_aggregation")), rc
