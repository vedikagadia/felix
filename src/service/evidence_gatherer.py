"""EvidenceGatherer — assemble the evidence felix reasons over for one alert.

The retrieval half of the agent loop, everything BEFORE the LLM. Given an alert
string it embeds the text once and gathers, into an EvidencePacket:
  1. semantically similar past incidents          (episodic memory)
  2. relevant documentation                        (project docs)
  3. recent code changes                           (the "what changed?" signal)
  4. optionally, an upstream graph trace from the node where the symptom
     originates, to find who up the call stack could be the real cause.

The reasoning step (hand the packet to the LLM for a Diagnosis) lives in
diagnoser.py (step 2); the EvidenceGatherer stops at the packet.
"""

from __future__ import annotations

import psycopg

from ..clients.embedder import Embedder, get_embedder
from ..models import EvidencePacket
from ..store.repositories import (
    ChangeRepository,
    DocRepository,
    GraphRepository,
    IncidentRepository,
    RunbookRepository,
)
from .topology_health import TopologyHealthService


class EvidenceGatherer:
    def __init__(self, conn: psycopg.Connection, embedder: Embedder | None = None):
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.incidents = IncidentRepository(conn)
        self.docs = DocRepository(conn)
        self.changes = ChangeRepository(conn)
        self.graph = GraphRepository(conn)
        self.runbooks = RunbookRepository(conn)
        self.health = TopologyHealthService(conn)

    def gather(
        self,
        alert: str,
        origin_node: str | None = None,
        k: int = 3,
        since_days: int = 14,
    ) -> EvidencePacket:
        qv = self.embedder.embed(alert)
        return EvidencePacket(
            alert=alert,
            incidents=self.incidents.recall(qv, k=k),
            docs=self.docs.recall(qv, k=k),
            changes=self.changes.recall(qv, k=k, since_days=since_days),
            upstream=self.graph.upstream_callers(origin_node, max_depth=4) if origin_node else [],
            # Live-metric correlation: reuse the SAME query vector already
            # computed above for runbook recall (never embed twice); correlate
            # downstream health only when the alert names a known service (the
            # service returns [] otherwise, so this is a no-op for unrelated
            # alerts — e.g. the deterministic write-back tests).
            runbooks=self.runbooks.recall(qv, k=k),
            topology_health=self.health.evaluate(alert, origin_node=origin_node),
        )
