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
    ActiveIncident,
    CodeChange,
    CodeNode,
    Diagnosis,
    DiagnosisResult,
    DocChunk,
    EvidencePacket,
    GraphHit,
    Incident,
    Message,
    NodeHealth,
    Project,
    Recall,
    Runbook,
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def project_to_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "display_name": p.display_name,
        "source_kind": p.source_kind,
        "source_ref": p.source_ref,
        "created_at": _iso(p.created_at),
        "last_synced": _iso(p.last_synced),
    }


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
        "feedback": inc.feedback,
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


def node_health_to_dict(nh: NodeHealth) -> dict[str, Any]:
    """One breached downstream health check -> the NodeHealth shape the panel
    renders as the live-metric-querying proof (which signal breached, observed
    vs. threshold, and how many samples backed it)."""
    return {
        "service": nh.service,
        "metric": nh.metric,
        "intent": nh.intent,
        "observed": nh.observed,
        "threshold": nh.threshold,
        "breached": nh.breached,
        "sample_count": nh.sample_count,
    }


def runbook_to_dict(rb: Runbook) -> dict[str, Any]:
    return {
        "id": rb.id,
        "title": rb.title,
        "symptoms": rb.symptoms,
        "service": rb.service,
        "tags": rb.tags,
        "created_at": _iso(rb.created_at),
        "steps": [
            {
                "step_order": s.step_order,
                "action": s.action,
                "command": s.command,
                "outcome": s.outcome,
            }
            for s in rb.steps
        ],
    }


def packet_to_dict(packet: EvidencePacket) -> dict[str, Any]:
    return {
        "alert": packet.alert,
        "incidents": [_recall_to_dict(r, incident_to_dict) for r in packet.incidents],
        "docs": [_recall_to_dict(r, doc_to_dict) for r in packet.docs],
        "changes": [_recall_to_dict(r, change_to_dict) for r in packet.changes],
        "upstream": [graph_hit_to_dict(h) for h in packet.upstream],
        # Live-metric correlation (the "what's ALSO unhealthy downstream right
        # now" proof) + curated runbooks recalled for the alert text.
        "topology_health": [node_health_to_dict(nh) for nh in packet.topology_health],
        "runbooks": [_recall_to_dict(r, runbook_to_dict) for r in packet.runbooks],
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
        "evidence_order": d.evidence_order,
    }


def message_to_dict(m: Message) -> dict[str, Any]:
    return {
        "text": m.text,
        "cited_incident_ids": m.cited_incident_ids,
        "cited_change_ids": m.cited_change_ids,
        "incident_id": m.incident_id,
        "evidence_order": m.evidence_order,
    }


def metric_sample_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """One live metric sample -> the MetricSample shape the panel expects.

    Accepts both a `MetricRepository.recent_samples` row (a datetime `ts`) and a
    decoded changefeed `after` payload (a string `ts`), coercing either to an
    ISO-8601 string so the two delivery paths (backfill + stream) look identical
    to the frontend."""
    ts = row.get("ts")
    value = row.get("value")
    return {
        "service": row.get("service"),
        "metric": row.get("metric"),
        "value": float(value) if value is not None else None,
        "ts": _iso(ts) if isinstance(ts, datetime) else ts,
        "labels": row.get("labels"),
    }


def alert_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """One `list_alerts` row -> the frozen AlertPayload (CDC_INTERFACE §7.3).

    `service`/`metric` are split out of the `"cdc:<service>:<metric>"` origin_node
    the watcher encodes; a missing or short origin_node degrades to null/null.
    `created_at` arrives as a psycopg datetime, so it's ISO-8601'd here."""
    service = metric = None
    origin_node = row.get("origin_node")
    if origin_node:
        parts = origin_node.split(":")
        if len(parts) == 3 and parts[0] == "cdc":
            _, service, metric = parts
    return {
        "session_id": row["id"],
        "service": service,
        "metric": metric,
        "summary": row["alert"],
        "created_at": _iso(row["created_at"]),
        "status": row["status"],
    }


def session_to_dict(session: ActiveIncident, incident: Incident | None) -> dict[str, Any]:
    """A session + its linked episodic incident -> the SessionResponse
    (CDC_INTERFACE §7.2). The diagnosis is reconstructed from the incident's
    root_cause + resolution_steps and the agent turn; `evidence` is null on this
    read (recall is not re-run — that would duplicate the diagnoser)."""
    agent_turn = next((t for t in session.turns if t.role == "agent"), None)
    diagnosis = None
    if incident is not None:
        diagnosis = diagnosis_to_dict(
            Diagnosis(
                summary=agent_turn.content if agent_turn is not None else "",
                root_cause=incident.root_cause,
                proposed_steps=[
                    {"action": s.action, "command": s.command, "outcome": s.outcome}
                    for s in incident.resolution_steps
                ],
                incident_id=incident.id,
            )
        )
    return {
        "session_id": session.id,
        "source": session.source,
        "status": session.status,
        "alert": session.alert,
        "origin_node": session.origin_node,
        "turns": [
            {"turn_order": t.turn_order, "role": t.role, "content": t.content}
            for t in session.turns
        ],
        "incident_id": session.incident_id,
        "diagnosis": diagnosis,
        "evidence": None,
    }


def result_to_dict(result: DiagnosisResult) -> dict[str, Any]:
    """The /chat response envelope: {response_type, diagnosis, message, evidence,
    session_id}.

    `response_type` ("diagnosis" | "message") tells the frontend which of
    `diagnosis`/`message` is populated (the other is null) — a full structured
    diagnosis, or a lightweight conversational reply for a follow-up.
    `session_id` is the active-incident conversation this turn belongs to; the
    frontend echoes it on the next request to continue the same conversation."""
    resp = result.response
    return {
        "response_type": result.response_type,
        "diagnosis": diagnosis_to_dict(resp) if isinstance(resp, Diagnosis) else None,
        "message": message_to_dict(resp) if isinstance(resp, Message) else None,
        "evidence": packet_to_dict(result.evidence),
        "session_id": result.session_id,
    }
