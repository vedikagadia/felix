"""FastAPI app — the HTTP driver over felix's service layer.

Thin adapter, no business logic: it opens a DB connection per request, delegates
to `EvidenceGatherer` / `IncidentDiagnoser`, and serializes the result to the
JSON contract in `frontend/src/api/types.ts` (see `schemas.py`). Routes are
declared `def` (not `async def`) so FastAPI runs them in a threadpool — the
psycopg and LLM calls are blocking, and this keeps the event loop free.

Run it with `python -m src serve` (see cli.py) or, for autoreload during dev:
    uvicorn src.api.app:app --reload --port 8000
"""

from __future__ import annotations

import json
import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..service.evidence_gatherer import EvidenceGatherer
from ..store.connection import get_conn
from .schemas import (
    alert_to_dict,
    incident_to_dict,
    metric_sample_to_dict,
    packet_to_dict,
    result_to_dict,
    session_to_dict,
)

# Sinkless CHANGEFEED for the live-monitoring stream — new samples only
# (`no_initial_scan`); the panel seeds history from GET /metrics/recent. Same
# feed the watcher uses, but this consumer only relays rows to the browser.
_METRICS_CHANGEFEED_SQL = "EXPERIMENTAL CHANGEFEED FOR metrics WITH updated, no_initial_scan"


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame: a named event + a JSON data line.
    Frames are separated by a blank line, per the SSE spec."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class ChatRequest(BaseModel):
    alert: str = Field(..., min_length=1, description="The alert / error message text.")
    origin_node: str | None = Field(
        default=None,
        description="code_nodes.name where the symptom surfaces; enables the upstream graph trace.",
    )
    k: int = Field(default=3, ge=1, le=20, description="Results per memory source.")
    session_id: str | None = Field(
        default=None,
        description="Active-incident conversation id from a prior /chat response; "
        "set it to ask a follow-up in the same conversation (multi-turn).",
    )


class FeedbackRequest(BaseModel):
    helpful: bool = Field(
        ...,
        description="True if the diagnosis was helpful (promote it into recallable "
        "memory), False if not (keep it out of recall).",
    )


def db_conn():
    """Per-request connection dependency. Mirrors the CLI's one-conn-per-run
    lifetime — the caller (this request) owns and closes it."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="felix",
        description="SRE incident-memory agent — HTTP API over the recall + reasoning loop.",
        version="0.1.0",
    )

    # CORS: allow the Vite dev origin(s). Override with FELIX_CORS_ORIGINS
    # (comma-separated) in prod; "*" is fine for a local, unauthenticated demo.
    origins = os.environ.get("FELIX_CORS_ORIGINS")
    allow_origins = [o.strip() for o in origins.split(",")] if origins else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/recall")
    def recall(req: ChatRequest, conn=Depends(db_conn)) -> dict:
        """Retrieval only — the `--no-llm` equivalent. Never writes, never calls
        the LLM. Returns {"evidence": EvidencePacket}."""
        gatherer = EvidenceGatherer(conn)
        packet = gatherer.gather(req.alert, origin_node=req.origin_node, k=req.k)
        return {"evidence": packet_to_dict(packet)}

    @app.get("/incidents")
    def list_incidents(limit: int = 200, conn=Depends(db_conn)) -> dict:
        """The whole incident library, newest-first, for the browse page. Pure
        read, no LLM. Returns {"incidents": [{item, distance}]} — `distance` is
        null here (this view isn't vector-ranked); the shape matches
        /incidents/search so the frontend renders one list either way."""
        from ..store.repositories import IncidentRepository

        rows = IncidentRepository(conn).list_all(limit=limit)
        return {"incidents": [{"item": incident_to_dict(i), "distance": None} for i in rows]}

    @app.get("/incidents/search")
    def search_incidents(q: str, k: int = 10, conn=Depends(db_conn)) -> dict:
        """Semantic search over the incident library — embeds `q` and ranks
        incidents by CockroachDB VECTOR distance (the showcase). Returns
        {"query", "incidents": [{item, distance}]} sorted nearest-first."""
        from ..clients.embedder import get_embedder
        from ..store.repositories import IncidentRepository

        qv = get_embedder().embed(q)
        hits = IncidentRepository(conn).search(qv, k=k)
        return {
            "query": q,
            "incidents": [{"item": incident_to_dict(h.item), "distance": h.distance} for h in hits],
        }

    @app.post("/incidents/{incident_id}/feedback")
    def incident_feedback(
        incident_id: str, req: FeedbackRequest, conn=Depends(db_conn)
    ) -> dict:
        """Record human feedback on a live-diagnosed incident — felix's learning
        loop. `helpful=true` embeds the incident (title + symptoms, the same text
        the seeder embeds) so it becomes recallable by future alerts; `false`
        clears the embedding so a wrong diagnosis is never recalled. 404 if the
        incident id is unknown. Needs the embedder (only when helpful) but not the
        LLM. Returns {incident_id, feedback, recallable}."""
        from ..store.repositories import ActionRepository, IncidentRepository

        repo = IncidentRepository(conn)
        incident = repo.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")

        embedding = None
        if req.helpful:
            from ..clients.embedder import get_embedder

            embedding = get_embedder().embed(f"{incident.title}\n{incident.symptoms}")

        # One transaction: promote/demote the row + write the audit trail together.
        with conn.transaction():
            repo.record_feedback(incident_id, helpful=req.helpful, embedding=embedding)
            ActionRepository(conn).log(
                action_type="feedback",
                tool_called="record_feedback",
                input={"incident_id": incident_id, "helpful": req.helpful},
                output={"recallable": req.helpful},
            )
        return {
            "incident_id": incident_id,
            "feedback": "helpful" if req.helpful else "not_helpful",
            "recallable": req.helpful,
        }

    @app.get("/alerts")
    def alerts(conn=Depends(db_conn)) -> dict:
        """Open cdc-sourced sessions, newest-first, as frozen AlertPayloads
        (CDC_INTERFACE §7.3). Pure read — the browser polls this every 3s; no
        LLM guard. Returns {"alerts": [AlertPayload, ...]}."""
        from ..store.repositories import ActiveIncidentRepository

        rows = ActiveIncidentRepository(conn).list_alerts(source="cdc", status="open")
        return {"alerts": [alert_to_dict(r) for r in rows]}

    @app.get("/metrics/config")
    def metrics_config() -> dict:
        """Default alert levels for the live-monitoring panel — the p99
        threshold each metric trips at. `default_p99_ms` applies to any latency
        metric without a specific entry; `thresholds` maps metric name -> its
        own p99 (from METRIC_ALERT_THRESHOLDS). The panel seeds each card's
        alert level from this; the operator can still override it live. No DB,
        no LLM."""
        settings = get_settings()
        return {
            "default_p99_ms": settings.metric_alert_default_p99_ms,
            "thresholds": settings.metric_alert_thresholds,
        }

    @app.get("/metrics/recent")
    def metrics_recent(
        limit: int = 200,
        service: str | None = None,
        metric: str | None = None,
        conn=Depends(db_conn),
    ) -> dict:
        """Recent metric samples for the live-monitoring panel's cold start —
        oldest-first so the frontend can seed each sparkline directly. Optional
        `service`/`metric` filters. Pure read; no LLM. Returns
        {"samples": [MetricSample, ...]}."""
        from ..store.repositories import MetricRepository

        rows = MetricRepository(conn).recent_samples(limit=limit, service=service, metric=metric)
        # recent_samples is newest-first; reverse to chronological for plotting.
        samples = [metric_sample_to_dict(r) for r in reversed(rows)]
        return {"samples": samples}

    @app.get("/metrics/stream")
    def metrics_stream(service: str | None = None, metric: str | None = None) -> StreamingResponse:
        """Live metric feed over Server-Sent Events — the CDC showcase for the
        live-monitoring panel. Holds a sinkless CHANGEFEED on `metrics` and emits
        one `sample` frame per new row (optionally filtered by service/metric):

          sample — {service, metric, value, ts, labels} for each inserted row
          error  — {"error": "..."} if the feed raises

        Uses its OWN connection (opened here, closed when the client disconnects
        and the generator is torn down): a changefeed holds a server-side
        streaming portal and can't share a connection, so this must not use the
        request-scoped `db_conn` dependency."""

        def event_stream():
            from ..store.connection import get_conn

            conn = get_conn()
            try:
                # Idempotent; rangefeeds must be enabled for a changefeed (on
                # locally already). Safe to run before the stream portal opens.
                with conn.cursor() as cur:
                    cur.execute("SET CLUSTER SETTING kv.rangefeed.enabled = true")
                with conn.cursor() as cur:
                    for row in cur.stream(_METRICS_CHANGEFEED_SQL):
                        payload = json.loads(row[2].decode())
                        after = payload.get("after")
                        if after is None:  # a delete — we only INSERT metrics
                            continue
                        if service and after.get("service") != service:
                            continue
                        if metric and after.get("metric") != metric:
                            continue
                        yield _sse("sample", metric_sample_to_dict(after))
            except Exception as e:  # noqa: BLE001 - report to the client, don't 500 mid-stream
                yield _sse("error", {"error": str(e)})
            finally:
                conn.close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/db/overview")
    def db_overview() -> dict:
        """A read-only snapshot of the CockroachDB cluster, gathered through the
        **CockroachDB Cloud Managed MCP Server** — felix acting as its own MCP
        client (`clients/cockroach_mcp`). Invokes only the read-only introspection
        tools (get_cluster / list_databases / list_tables / show_running_queries).

        Degrades gracefully: if the MCP endpoint isn't configured or the
        connection/auth fails, returns 200 with `{"connected": false, "reason":
        ...}` so the panel can render a friendly state instead of erroring. On
        success returns `{"connected": true, "source", "cluster", "databases",
        "tables_by_db", "running_queries", "tools_used"}`. No DB-URL connection,
        no LLM — this path talks to the cluster purely over MCP."""
        from ..clients import cockroach_mcp

        settings = get_settings()
        if not settings.crdb_mcp_url or not settings.crdb_mcp_cluster_id:
            return {
                "connected": False,
                "reason": "CockroachDB MCP not configured (set CRDB_MCP_URL + CRDB_MCP_CLUSTER_ID).",
            }
        try:
            overview = cockroach_mcp.fetch_overview()
        except Exception as e:  # noqa: BLE001 - surface as a soft state, not a 500
            return {
                "connected": False,
                "reason": f"Could not reach the MCP server: {e}. "
                "Authenticate once with `python -m src mcp-probe`.",
            }
        return {"connected": True, "source": "cockroachdb-cloud-mcp", **overview}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, conn=Depends(db_conn)) -> dict:
        """A session's transcript + the diagnosis reconstructed from its linked
        episodic incident (CDC_INTERFACE §7.2). 404 if unknown. Pure read — no
        LLM guard, no recall re-run (`evidence` is null)."""
        from ..store.repositories import ActiveIncidentRepository, IncidentRepository

        session = ActiveIncidentRepository(conn).get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        incident = (
            IncidentRepository(conn).get(session.incident_id)
            if session.incident_id is not None
            else None
        )
        return session_to_dict(session, incident)

    @app.post("/chat")
    def chat(req: ChatRequest, conn=Depends(db_conn)) -> dict:
        """Full loop: recall -> reason -> write-back. Returns
        {"diagnosis": Diagnosis, "evidence": EvidencePacket}."""
        settings = get_settings()
        if settings.llm_provider == "gemini" and not settings.gemini_api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM not configured: set GEMINI_API_KEY (or switch LLM_PROVIDER). "
                "Use POST /recall for evidence without a diagnosis.",
            )

        # Imported here (not at module top) so importing the app never pulls in
        # the LLM SDK / repositories unless a /chat actually runs — matches the
        # lazy-client pattern used across clients/.
        from ..clients.llm import get_llm
        from ..service.diagnoser import IncidentDiagnoser
        from ..store.repositories import (
            ActionRepository,
            ActiveIncidentRepository,
            IncidentRepository,
        )

        gatherer = EvidenceGatherer(conn)
        diagnoser = IncidentDiagnoser(
            gatherer,
            get_llm(),
            IncidentRepository(conn),
            ActionRepository(conn),
            ActiveIncidentRepository(conn),
        )
        result = diagnoser.respond(
            req.alert, origin_node=req.origin_node, k=req.k, session_id=req.session_id
        )
        return result_to_dict(result)

    @app.post("/chat/stream")
    def chat_stream(req: ChatRequest, conn=Depends(db_conn)) -> StreamingResponse:
        """Streaming twin of /chat: the full loop, but the diagnosis is delivered
        as Server-Sent Events so the UI can show recall + reasoning live.

        Frames (each `event:`/`data:` pair):
          evidence — {"evidence": EvidencePacket} once, after recall (fills the
              evidence panel while the model is still generating)
          delta    — {"text": "..."} for each chunk of the model's output
          done     — {"diagnosis", "evidence", "session_id"} — same envelope as
              /chat, after parse + write-back
          error    — {"error": "..."} if the loop raises mid-stream

        Same 503 contract as /chat when the LLM isn't configured."""
        settings = get_settings()
        if settings.llm_provider == "gemini" and not settings.gemini_api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM not configured: set GEMINI_API_KEY (or switch LLM_PROVIDER). "
                "Use POST /recall for evidence without a diagnosis.",
            )

        from ..clients.llm import get_llm
        from ..service.diagnoser import IncidentDiagnoser
        from ..store.repositories import (
            ActionRepository,
            ActiveIncidentRepository,
            IncidentRepository,
        )

        gatherer = EvidenceGatherer(conn)
        diagnoser = IncidentDiagnoser(
            gatherer,
            get_llm(),
            IncidentRepository(conn),
            ActionRepository(conn),
            ActiveIncidentRepository(conn),
        )

        def event_stream():
            # The connection is owned by the db_conn dependency and closed when
            # this generator is exhausted (StreamingResponse drives it to the
            # end). Map each service event to an SSE frame; surface any error as
            # a terminal `error` frame instead of a bare 500 mid-stream.
            try:
                for kind, payload in diagnoser.respond_stream(
                    req.alert, origin_node=req.origin_node, k=req.k, session_id=req.session_id
                ):
                    if kind == "evidence":
                        yield _sse("evidence", {"evidence": packet_to_dict(payload)})
                    elif kind == "delta":
                        yield _sse("delta", {"text": payload})
                    elif kind == "done":
                        # Same envelope as POST /chat (response_type + diagnosis
                        # | message + evidence + session_id).
                        yield _sse("done", result_to_dict(payload))
            except Exception as e:  # noqa: BLE001 - report any failure to the client, don't 500 mid-stream
                yield _sse("error", {"error": str(e)})

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            # Disable proxy buffering so deltas reach the browser as they're sent.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
