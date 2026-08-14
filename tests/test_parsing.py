"""Deterministic tests for the diagnoser's parsing + guards — no DB, no network.

These feed KNOWN LLM output into the parsing code and assert the exact
transform. Because we supply the input, the expected output is known precisely
(unlike diagnosis quality, which is fuzzy and lives in test_puzzles.py).
"""

from __future__ import annotations

import json

import pytest

from src.models import Diagnosis, Message
from src.service.diagnoser import IncidentDiagnoser, _extract_json_object
from tests.conftest import make_packet


@pytest.fixture
def diagnoser(fake_llm):
    # Parsing methods use no repo/gatherer state, so Nones are safe here.
    return IncidentDiagnoser(gatherer=None, llm=fake_llm, incident_repo=None, action_repo=None)


# ── _extract_json_object ─────────────────────────────────────────────────────


def test_extract_plain_json():
    assert _extract_json_object('{"summary": "s"}') == {"summary": "s"}


def test_extract_fenced_json():
    text = 'here you go:\n```json\n{"summary": "s"}\n```\n'
    assert _extract_json_object(text) == {"summary": "s"}


def test_extract_prose_then_object():
    text = 'The problem is clear. {"summary": "s", "confidence": 0.9}'
    assert _extract_json_object(text) == {"summary": "s", "confidence": 0.9}


def test_extract_garbage_returns_none():
    assert _extract_json_object("no json here at all") is None


def test_extract_prefers_answer_block_over_example():
    # Model quotes an example fenced block first, then gives its real answer.
    # The real answer (the one shaped like a Diagnosis, with "summary") wins.
    text = (
        "For example:\n```json\n{\"foo\": 1}\n```\n"
        "My answer:\n```json\n{\"summary\": \"real\", \"root_cause\": \"c\"}\n```"
    )
    obj = _extract_json_object(text)
    assert obj["summary"] == "real"


# ── _parse_response (allow_message=False): citation-integrity guard ───────────


def test_citation_guard_drops_hallucinated_ids(diagnoser):
    packet = make_packet(incident_ids=["real-inc"], change_ids=["real-chg"])
    text = json.dumps(
        {
            "summary": "s",
            "root_cause": "c",
            "proposed_steps": [],
            "cited_incident_ids": ["real-inc", "HALLUCINATED"],
            "cited_change_ids": ["real-chg", "ALSO-FAKE"],
            "confidence": 0.9,
        }
    )
    d = diagnoser._parse_response(text, packet, allow_message=False)
    assert d.cited_incident_ids == ["real-inc"]
    assert d.cited_change_ids == ["real-chg"]


def test_citation_guard_empty_when_none_match(diagnoser):
    packet = make_packet(incident_ids=["real-inc"])
    text = json.dumps({"summary": "s", "cited_incident_ids": ["nope"], "cited_change_ids": []})
    d = diagnoser._parse_response(text, packet, allow_message=False)
    assert d.cited_incident_ids == []


# ── _parse_response (allow_message=False): confidence clamp + type coercion ───


@pytest.mark.parametrize(
    "raw,expected",
    [(1.5, 1.0), (-0.2, 0.0), (0.5, 0.5), ("high", None), (None, None), (True, None)],
)
def test_confidence_clamp(diagnoser, raw, expected):
    packet = make_packet()
    text = json.dumps({"summary": "s", "confidence": raw})
    d = diagnoser._parse_response(text, packet, allow_message=False)
    assert d.confidence == expected


def test_non_json_falls_back_to_summary(diagnoser):
    packet = make_packet()
    d = diagnoser._parse_response("model babbled with no json", packet, allow_message=False)
    assert isinstance(d, Diagnosis)
    assert d.root_cause is None
    assert d.cited_incident_ids == []
    assert "babbled" in d.summary


def test_root_cause_null_preserved(diagnoser):
    packet = make_packet()
    text = json.dumps({"summary": "s", "root_cause": None})
    d = diagnoser._parse_response(text, packet, allow_message=False)
    assert d.root_cause is None


# ── _parse_response (allow_message=True): the follow-up tagged-union branch ───


def test_follow_up_message_type_parses_as_message(diagnoser):
    packet = make_packet()
    text = json.dumps({"type": "message", "text": "Run `python -m src serve` to restart it."})
    r = diagnoser._parse_response(text, packet, allow_message=True)
    assert isinstance(r, Message)
    assert "restart it" in r.text


def test_follow_up_diagnosis_type_still_parses_as_diagnosis(diagnoser):
    # A follow-up may escalate back to a full diagnosis; the discriminator wins.
    packet = make_packet()
    text = json.dumps({"type": "diagnosis", "summary": "new problem", "root_cause": "c"})
    r = diagnoser._parse_response(text, packet, allow_message=True)
    assert isinstance(r, Diagnosis)
    assert r.root_cause == "c"


def test_follow_up_untyped_text_shape_defaults_to_message(diagnoser):
    # No "type", but free "text" and no "summary" → treated as a message.
    packet = make_packet()
    text = json.dumps({"text": "just re-run the service"})
    r = diagnoser._parse_response(text, packet, allow_message=True)
    assert isinstance(r, Message)


def test_follow_up_non_json_falls_back_to_message(diagnoser):
    packet = make_packet()
    r = diagnoser._parse_response("model babbled with no json", packet, allow_message=True)
    assert isinstance(r, Message)
    assert "babbled" in r.text


def test_message_citation_guard_drops_hallucinated_ids(diagnoser):
    packet = make_packet(incident_ids=["real-inc"], change_ids=["real-chg"])
    text = json.dumps(
        {
            "type": "message",
            "text": "see the prior incident",
            "cited_incident_ids": ["real-inc", "FAKE"],
            "cited_change_ids": ["ALSO-FAKE"],
        }
    )
    r = diagnoser._parse_response(text, packet, allow_message=True)
    assert isinstance(r, Message)
    assert r.cited_incident_ids == ["real-inc"]
    assert r.cited_change_ids == []


# ── _to_resolution_steps: list[str] vs list[dict] ────────────────────────────


def test_steps_from_dicts():
    raw = [{"action": "do a", "command": "cmd", "outcome": "ok"}]
    steps = IncidentDiagnoser._to_resolution_steps(raw)
    assert len(steps) == 1
    assert steps[0].step_order == 1
    assert steps[0].action == "do a"
    assert steps[0].command == "cmd"
    assert steps[0].outcome == "ok"


def test_steps_from_plain_strings():
    steps = IncidentDiagnoser._to_resolution_steps(["restart it", "scale up"])
    assert [s.action for s in steps] == ["restart it", "scale up"]
    assert [s.step_order for s in steps] == [1, 2]
    assert steps[0].command is None


def test_steps_empty():
    assert IncidentDiagnoser._to_resolution_steps([]) == []
    assert IncidentDiagnoser._to_resolution_steps(None) == []
