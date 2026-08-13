"""API-team tests — GET /alerts and GET /sessions/{id} shape checks.

No DB or LLM: the db_conn dependency is overridden to a sentinel and the two
repositories the endpoints construct are monkeypatched to return canned domain
objects. This pins the frozen AlertPayload (CDC_INTERFACE §7.3) and
SessionResponse (§7.2) shapes without network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.store.repositories as repos
from src.api.app import create_app, db_conn
from src.models import ActiveIncident, ActiveIncidentTurn, Incident, ResolutionStep


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[db_conn] = lambda: None
    return TestClient(app)


class _FakeActiveRepo:
    def __init__(self, conn):
        pass

    def list_alerts(self, source="cdc", status="open"):
        return [
            {
                "id": "sess-1",
                "alert": "p99 checkout_latency_ms for checkout-service spiked to 2140ms "
                "over the last 60 samples while avg held flat at 95ms — no dashboard alert fired.",
                "origin_node": "cdc:checkout-service:checkout_latency_ms",
                "source": "cdc",
                "status": "open",
                "created_at": datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc),
            }
        ]

    def get_session(self, session_id):
        if session_id != "sess-1":
            return None
        return ActiveIncident(
            id="sess-1",
            alert="p99 spiked",
            origin_node="cdc:checkout-service:checkout_latency_ms",
            incident_id="inc-1",
            status="open",
            turns=[
                ActiveIncidentTurn(turn_order=1, role="user", content="p99 spiked"),
                ActiveIncidentTurn(turn_order=2, role="agent", content="tail latency hidden by avg"),
            ],
            source="cdc",
        )


class _FakeIncidentRepo:
    def __init__(self, conn):
        pass

    def get(self, incident_id):
        return Incident(
            id="inc-1",
            title="checkout latency",
            symptoms="slow checkout",
            root_cause="LATENCY_AGGREGATION flipped to avg",
            resolution_steps=[
                ResolutionStep(step_order=1, action="revert metrics.py", command="git revert", outcome=None)
            ],
        )


@pytest.fixture(autouse=True)
def _patch_repos(monkeypatch):
    monkeypatch.setattr(repos, "ActiveIncidentRepository", _FakeActiveRepo)
    monkeypatch.setattr(repos, "IncidentRepository", _FakeIncidentRepo)


def test_alerts_shape(client):
    body = client.get("/alerts").json()
    assert list(body.keys()) == ["alerts"]
    payload = body["alerts"][0]
    assert set(payload) == {"session_id", "service", "metric", "summary", "created_at", "status"}
    assert payload["session_id"] == "sess-1"
    assert payload["service"] == "checkout-service"
    assert payload["metric"] == "checkout_latency_ms"
    assert payload["status"] == "open"
    assert payload["created_at"] == "2026-08-13T12:00:00+00:00"


def test_session_shape(client):
    body = client.get("/sessions/sess-1").json()
    assert set(body) == {
        "session_id", "source", "status", "alert", "origin_node",
        "turns", "incident_id", "diagnosis", "evidence",
    }
    assert body["evidence"] is None
    assert body["source"] == "cdc"
    assert [t["turn_order"] for t in body["turns"]] == [1, 2]
    diag = body["diagnosis"]
    assert diag["summary"] == "tail latency hidden by avg"
    assert diag["root_cause"] == "LATENCY_AGGREGATION flipped to avg"
    assert diag["incident_id"] == "inc-1"
    assert diag["proposed_steps"] == [
        {"action": "revert metrics.py", "command": "git revert", "outcome": None}
    ]


def test_session_unknown_404(client):
    assert client.get("/sessions/nope").status_code == 404
