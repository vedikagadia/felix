"""Shared test fixtures + a FakeLLM.

The FakeLLM is the whole point of the deterministic suite: IncidentDiagnoser
takes an LLMClient via its constructor, so a fake that returns scripted text
(and records the prompt it was handed) lets us test all the code AROUND the
model — JSON parsing, the citation-integrity guard, write-back, graceful
failure — with no API key and no network. It cannot (and must not) be used to
judge diagnosis QUALITY; that's what the live-marked puzzle tests are for.
"""

from __future__ import annotations

import pytest

from src.clients.llm import LLMClient
from src.models import (
    CodeChange,
    CodeNode,
    DocChunk,
    EvidencePacket,
    GraphHit,
    Incident,
    LLMResult,
    Recall,
)


class FakeLLM(LLMClient):
    """Returns a scripted completion; records every (prompt, system) it saw.

    Pass `text` for a fixed response, or `responses` (a list) to return a
    different completion per call. `.calls` lets a test assert the diagnoser
    built the prompt correctly (e.g. that a recalled id was included).
    """

    def __init__(
        self,
        text: str | None = None,
        *,
        responses: list[str] | None = None,
        model: str = "fake-model",
        input_tokens: int | None = 11,
        output_tokens: int | None = 22,
    ):
        if responses is None:
            responses = [text if text is not None else "{}"]
        self._responses = responses
        self._model = model
        self._in = input_tokens
        self._out = output_tokens
        self.calls: list[tuple[str, str | None]] = []

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResult:
        self.calls.append((prompt, system))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return LLMResult(
            text=self._responses[idx],
            model=self._model,
            input_tokens=self._in,
            output_tokens=self._out,
        )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


def make_packet(
    *,
    alert: str = "test alert",
    incident_ids: list[str] | None = None,
    change_ids: list[str] | None = None,
    with_upstream: bool = False,
) -> EvidencePacket:
    """Hand-build an EvidencePacket with known ids, so parsing/citation tests
    have a fixed ground truth to assert against (no DB, no embedder)."""
    incidents = [
        Recall(
            item=Incident(id=i, title=f"incident {i}", symptoms="db.pool.exhausted"),
            distance=0.3,
        )
        for i in (incident_ids or [])
    ]
    changes = [
        Recall(
            item=CodeChange(
                id=c,
                commit_sha="deadbeef",
                merged_at=__import__("datetime").datetime(2026, 8, 7),
                title=f"change {c}",
            ),
            distance=0.4,
        )
        for c in (change_ids or [])
    ]
    upstream = []
    if with_upstream:
        upstream = [
            GraphHit(node=CodeNode(id="n1", name="ConnectionPool.acquire", kind="function"), depth=0),
        ]
    return EvidencePacket(alert=alert, incidents=incidents, changes=changes, upstream=upstream)
