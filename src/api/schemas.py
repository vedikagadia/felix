"""Serialization: domain models -> the JSON shape the frontend expects.

Mirrors `frontend/src/api/types.ts`. Recalls serialize to `{item, distance}` and
graph hits to `{node, depth}` — exactly the field names the domain models
already use, so this is mostly a datetime-and-nesting pass. Kept as plain
functions (not Pydantic models) so the API layer stays a thin adapter with no
second copy of the domain vocabulary to keep in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import (
    CodeChange,
    CodeNode,
    Diagnosis,
    DiagnosisResult,
    DocChunk,
    EvidencePacket,
    GraphHit,
    Incident,
    Recall,
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def incident_to_dict(inc: Incident) -> dict[str, Any]:
    return {
        "id": inc.id,
        "title": inc.title,
        "symptoms": inc.symptoms,
        "root_cause": inc.root_cause,
        "service": inc.service,
        "severity": inc.severity,
        "tags": inc.tags,
        "occurred_at": _iso(inc.occurred_at),
        "resolution_steps": [
            {
                "step_order": s.step_order,
                "action": s.action,
                "command": s.command,
                "outcome": s.outcome,
            }
            for s in inc.resolution_steps
        ],
    }


def doc_to_dict(doc: DocChunk) -> dict[str, Any]:
    return {
        "id": doc.id,
        "doc_title": doc.doc_title,
        "heading": doc.heading,
        "body": doc.body,
        "doc_type": doc.doc_type,
        "source_path": doc.source_path,
    }


def change_to_dict(chg: CodeChange) -> dict[str, Any]:
    return {
        "id": chg.id,
        "commit_sha": chg.commit_sha,
        "merged_at": _iso(chg.merged_at),
        "title": chg.title,
        "summary": chg.summary,
        "author": chg.author,
        "files_changed": chg.files_changed,
        "services_affected": chg.services_affected,
        "affected_components": chg.affected_components,
    }


def node_to_dict(node: CodeNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "kind": node.kind,
        "file": node.file,
        "service": node.service,
        "source": node.source,
        "summary": node.summary,
        "last_commit": node.last_commit,
    }


def _recall_to_dict(r: Recall, item_fn) -> dict[str, Any]:
    return {"item": item_fn(r.item), "distance": r.distance}


def graph_hit_to_dict(hit: GraphHit) -> dict[str, Any]:
    return {"node": node_to_dict(hit.node), "depth": hit.depth}


def packet_to_dict(packet: EvidencePacket) -> dict[str, Any]:
    return {
        "alert": packet.alert,
        "incidents": [_recall_to_dict(r, incident_to_dict) for r in packet.incidents],
        "docs": [_recall_to_dict(r, doc_to_dict) for r in packet.docs],
        "changes": [_recall_to_dict(r, change_to_dict) for r in packet.changes],
        "upstream": [graph_hit_to_dict(h) for h in packet.upstream],
    }


def diagnosis_to_dict(d: Diagnosis) -> dict[str, Any]:
    return {
        "summary": d.summary,
        "root_cause": d.root_cause,
        "proposed_steps": d.proposed_steps,
        "cited_incident_ids": d.cited_incident_ids,
        "cited_change_ids": d.cited_change_ids,
        "confidence": d.confidence,
        "incident_id": d.incident_id,
    }


def result_to_dict(result: DiagnosisResult) -> dict[str, Any]:
    """The /chat response envelope: {diagnosis, evidence}."""
    return {
        "diagnosis": diagnosis_to_dict(result.diagnosis),
        "evidence": packet_to_dict(result.evidence),
    }
