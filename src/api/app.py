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
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
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
    project_to_dict,
    result_to_dict,
    session_to_dict,
)

# Sinkless CHANGEFEED for the live-monitoring stream — new samples only
# (`no_initial_scan`); the panel seeds history from GET /metrics/recent. Same
# feed the watcher uses, but this consumer only relays rows to the browser.
_METRICS_CHANGEFEED_SQL = "EXPERIMENTAL CHANGEFEED FOR metrics WITH updated, no_initial_scan"


log = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    """Is the boolean env var `name` turned on? One truthy set shared by every
    FELIX_* flag (matches Settings.cli_enabled) so `=on` never silently no-ops
    on one flag while working on another. Absent/unset reads as off."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _watcher_enabled() -> bool:
    """Run the CDC watcher in-process? Off by default (local `serve` is a plain
    API). The merged web+watch deploy sets FELIX_RUN_WATCHER=1 so one process —
    and one shared embedding model — serves the API AND holds the changefeed
    (DEPLOY.md §4). Standalone `python -m src watch` is unaffected."""
    return _env_flag("FELIX_RUN_WATCHER")


def _sample_enabled() -> bool:
    """Run the sample-traffic driver in-process? Off by default (local `serve`
    is a plain API). The deployed demo sets FELIX_RUN_SAMPLE=1 so the web task
    also drives real checkout traffic, which is what feeds the metrics the CDC
    watcher reacts to — no separate sample task needed. Standalone
    `python -m sample_project.run` is unaffected."""
    return _env_flag("FELIX_RUN_SAMPLE")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Optionally start the CDC watcher and/or the sample-traffic driver on
    their own background threads for the lifetime of the server. Each shares
    the process embedder but owns its own DB connection(s); on shutdown
    (SIGTERM from Fargate) both are torn down cleanly, independently of
    whether the other was enabled."""
    watcher = None
    if _watcher_enabled():
        from ..service.watcher import BackgroundWatcher

        log.info("FELIX_RUN_WATCHER set — starting in-process CDC watcher")
        watcher = BackgroundWatcher()
        watcher.start()

    traffic_driver = None
    if _sample_enabled():
        from sample_project.run import BackgroundTrafficDriver

        log.info("FELIX_RUN_SAMPLE set — starting in-process sample-traffic driver")
        traffic_driver = BackgroundTrafficDriver()
        traffic_driver.start()

    # Expose the thread handles so /health can report their liveness. Absent
    # (None) means "not enabled in this process"; present means "should be
    # running" — a False is_alive() then signals a silently-dead thread.
    app.state.watcher = watcher
    app.state.traffic_driver = traffic_driver

    try:
        yield
    finally:
        if watcher is not None:
            log.info("stopping in-process CDC watcher")
            watcher.stop()
        if traffic_driver is not None:
            log.info("stopping in-process sample-traffic driver")
            traffic_driver.stop()


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame: a named event + a JSON data line.
    Frames are separated by a blank line, per the SSE spec."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class ChatRequest(BaseModel):
    alert: str = Field(
        ..., min_length=1, max_length=8000, description="The alert / error message text."
    )
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


class DbPlanRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="A natural-language DB request (e.g. 'add a table for on-call "
        "schedules'). felix maps it to ONE CockroachDB MCP tool call for review — "
        "it is not executed here.",
    )


class DbExecuteRequest(BaseModel):
    tool: str = Field(..., description="The MCP tool to run — must be in the NL allowlist.")
    args: dict = Field(..., description="The tool's arguments, exactly as previewed in the plan.")


class OnboardRequest(BaseModel):
    source: str = Field(
        ...,
        min_length=1,
        description="A local directory path or a git URL to onboard as a project.",
    )
    name: str | None = Field(default=None, description="Display name (defaults to the repo/dir name).")
    project: str | None = Field(default=None, description="Project slug (defaults to a slug of the name).")
    sources: list[str] | None = Field(
        default=None,
        description="Subset of code/changes/docs/runbooks to ingest (default: all).",
    )
    max_commits: int = Field(default=200, ge=1, le=5000, description="git-log commits to ingest.")


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
        lifespan=lifespan,
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
        """Liveness of the API plus any in-process background threads.

        For each optional daemon (CDC watcher, sample-traffic driver) reports:
          - null    — not enabled in this process (nothing to supervise)
          - true    — enabled and its thread is running
          - false   — enabled but its thread has died (crash logged in `_run`)
        `status` is "ok" unless an enabled thread is no longer alive, in which
        case it's "degraded" — so a supervisor (or the demo) can see a silently
        dead watcher/driver instead of a green light over a broken background."""

        def _liveness(obj) -> bool | None:
            return obj.is_alive() if obj is not None else None

        threads = {
            "watcher": _liveness(getattr(app.state, "watcher", None)),
            "traffic_driver": _liveness(getattr(app.state, "traffic_driver", None)),
        }
        degraded = any(alive is False for alive in threads.values())
        return {"status": "degraded" if degraded else "ok", "threads": threads}

    @app.get("/projects")
    def list_projects(conn=Depends(db_conn)) -> dict:
        """Every onboarded project (the built-in demo first, then newest-first) —
        powers the header project switcher. Pure read, no LLM. Returns
        {"projects": [Project, ...]}."""
        from ..store.repositories import ProjectRepository

        rows = ProjectRepository(conn).list_projects()
        return {"projects": [project_to_dict(p) for p in rows]}

    @app.post("/projects/onboard")
    def onboard_project(req: OnboardRequest, conn=Depends(db_conn)) -> dict:
        """Onboard another project (local path or git URL) into felix's memory:
        parse its code graph and ingest its git log / docs / runbooks under a new
        project namespace, then register it so the switcher lists it. Runs in the
        threadpool (declared `def`) since ingest + embedding is blocking.

        Returns {"project", "display_name", "source_kind", "source_ref",
        "counts": {...}} on success; 400 on a bad source (missing path, clone
        failure, or the reserved 'sample' slug)."""
        from ..service.onboarding import ALL_SOURCES, OnboardingService

        sources = tuple(req.sources) if req.sources else ALL_SOURCES
        try:
            result = OnboardingService(conn).onboard(
                req.source,
                display_name=req.name,
                project=req.project,
                sources=sources,
                max_commits=req.max_commits,
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "project": result.project,
            "display_name": result.display_name,
            "source_kind": result.source_kind,
            "source_ref": result.source_ref,
            "counts": result.counts,
        }

    @app.post("/recall")
    def recall(req: ChatRequest, project: str = "sample", conn=Depends(db_conn)) -> dict:
        """Retrieval only — the `--no-llm` equivalent. Never writes, never calls
        the LLM. Returns {"evidence": EvidencePacket}. `project` (query param)
        scopes recall to one onboarded project's memory (default: the demo)."""
        gatherer = EvidenceGatherer(conn, project=project)
        packet = gatherer.gather(req.alert, origin_node=req.origin_node, k=req.k)
        return {"evidence": packet_to_dict(packet)}

    @app.get("/incidents")
    def list_incidents(limit: int = 200, project: str = "sample", conn=Depends(db_conn)) -> dict:
        """The whole incident library for `project`, newest-first, for the browse
        page. Pure read, no LLM. Returns {"incidents": [{item, distance}]} —
        `distance` is null here (this view isn't vector-ranked); the shape matches
        /incidents/search so the frontend renders one list either way."""
        from ..store.repositories import IncidentRepository

        rows = IncidentRepository(conn, project).list_all(limit=limit)
        return {"incidents": [{"item": incident_to_dict(i), "distance": None} for i in rows]}

    @app.get("/incidents/search")
    def search_incidents(
        q: str = Query(..., min_length=1, max_length=8000),
        k: int = 10,
        project: str = "sample",
        conn=Depends(db_conn),
    ) -> dict:
        """Semantic search over `project`'s incident library — embeds `q` and
        ranks incidents by CockroachDB VECTOR distance (the showcase). Returns
        {"query", "incidents": [{item, distance}]} sorted nearest-first."""
        from ..clients.embedder import get_embedder
        from ..store.repositories import IncidentRepository

        qv = get_embedder().embed(q)
        hits = IncidentRepository(conn, project).search(qv, k=k)
        return {
            "query": q,
            "incidents": [{"item": incident_to_dict(h.item), "distance": h.distance} for h in hits],
        }

    @app.post("/incidents/{incident_id}/feedback")
    def incident_feedback(
        incident_id: str, req: FeedbackRequest, project: str = "sample", conn=Depends(db_conn)
    ) -> dict:
        """Record human feedback on a live-diagnosed incident — felix's learning
        loop. `helpful=true` embeds the incident (title + symptoms, the same text
        the seeder embeds) so it becomes recallable by future alerts; `false`
        clears the embedding so a wrong diagnosis is never recalled. 404 if the
        incident id is unknown. Needs the embedder (only when helpful) but not the
        LLM. Returns {incident_id, feedback, recallable}."""
        from ..store.repositories import ActionRepository, IncidentRepository

        repo = IncidentRepository(conn, project)
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
            ActionRepository(conn, project).log(
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
    def alerts(project: str = "sample", conn=Depends(db_conn)) -> dict:
        """Open cdc-sourced sessions for `project`, newest-first, as frozen
        AlertPayloads (CDC_INTERFACE §7.3). Pure read — the browser polls this
        every 3s; no LLM guard. Returns {"alerts": [AlertPayload, ...]}."""
        from ..store.repositories import ActiveIncidentRepository

        rows = ActiveIncidentRepository(conn, project).list_alerts(source="cdc", status="open")
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
        project: str = "sample",
        conn=Depends(db_conn),
    ) -> dict:
        """Recent metric samples for `project`'s live-monitoring cold start —
        oldest-first so the frontend can seed each sparkline directly. Optional
        `service`/`metric` filters. Pure read; no LLM. Returns
        {"samples": [MetricSample, ...]}."""
        from ..store.repositories import MetricRepository

        rows = MetricRepository(conn, project).recent_samples(limit=limit, service=service, metric=metric)
        # recent_samples is newest-first; reverse to chronological for plotting.
        samples = [metric_sample_to_dict(r) for r in reversed(rows)]
        return {"samples": samples}

    @app.get("/metrics/stream")
    def metrics_stream(
        service: str | None = None, metric: str | None = None, project: str = "sample"
    ) -> StreamingResponse:
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
                        if after.get("project", "sample") != project:
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

    @app.post("/db/plan")
    def db_plan(req: DbPlanRequest) -> dict:
        """Step 1 of the DB-write flow (preview-then-confirm): map a
        natural-language request to ONE CockroachDB MCP tool call, WITHOUT
        running it. felix's LLM has no native tool-calling, so this prompts the
        model to emit a strict {tool, args} JSON, validates it against the
        additive-write allowlist (`cockroach_mcp.NL_TOOLS`), and returns the plan
        for the operator to review. The operator then POSTs it to /db/execute.

        Returns {"plan": {tool, args, explanation, write} } on a mappable request,
        or {"plan": null, "reason": ...} when it can't be mapped safely. 503 if
        the LLM isn't configured; 503 if MCP isn't configured (nothing to run
        against). No DB-URL connection, no write happens here."""
        settings = get_settings()
        if not settings.crdb_mcp_url or not settings.crdb_mcp_cluster_id:
            raise HTTPException(
                status_code=503,
                detail="CockroachDB MCP not configured (set CRDB_MCP_URL + CRDB_MCP_CLUSTER_ID).",
            )
        if settings.llm_provider == "gemini" and not settings.gemini_api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM not configured: set GEMINI_API_KEY (or switch LLM_PROVIDER).",
            )

        from ..clients.llm import get_llm
        from ..service.db_assistant import plan_operation

        plan = plan_operation(get_llm(), req.instruction)
        if plan.get("tool") is None:
            return {"plan": None, "reason": plan.get("reason") or "Could not map the request to a tool."}
        return {"plan": plan}

    @app.post("/db/execute")
    def db_execute(req: DbExecuteRequest) -> dict:
        """Step 2 of the DB-write flow: run the previewed plan against the cluster
        over MCP. The tool is re-validated against the additive-write allowlist
        (`cockroach_mcp.NL_TOOLS`) so a client can't smuggle in an off-list tool.

        Returns the raw run result: {ok, tool, args, result} on success or
        {ok:false, tool, args, error} on a tool/MCP error (still HTTP 200 — the
        panel shows the error and lets the operator retry). 503 if MCP isn't
        configured; 403 if the tool isn't permitted."""
        from ..clients import cockroach_mcp

        settings = get_settings()
        if not settings.crdb_mcp_url or not settings.crdb_mcp_cluster_id:
            raise HTTPException(
                status_code=503,
                detail="CockroachDB MCP not configured (set CRDB_MCP_URL + CRDB_MCP_CLUSTER_ID).",
            )
        if req.tool not in cockroach_mcp.NL_TOOLS:
            raise HTTPException(status_code=403, detail=f"tool {req.tool!r} is not permitted")

        return cockroach_mcp.run_tool(req.tool, req.args)

    @app.get("/cli/status")
    def cli_status() -> dict:
        """Whether the CLI panel's terminal is available, and whether `ccloud` is
        installed / authed on the API host — so the panel can render a banner
        ("ccloud not installed", auth hint) instead of a blank shell. Pure
        introspection: no shell is spawned here (that's the WS /cli/ws path)."""
        import shutil
        import subprocess

        settings = get_settings()
        ccloud_path = shutil.which("ccloud")
        account = None
        if ccloud_path:
            try:
                out = subprocess.run(
                    [ccloud_path, "auth", "whoami"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if out.returncode == 0:
                    account = out.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                account = None
        return {
            "enabled": settings.cli_enabled,
            "ccloud_installed": ccloud_path is not None,
            "ccloud_path": ccloud_path,
            "account": account,
            "cluster_id": settings.crdb_mcp_cluster_id,
        }

    @app.websocket("/cli/ws")
    async def cli_ws(ws: WebSocket) -> None:
        """Interactive terminal: bridges a real PTY (a login shell with `ccloud`
        on PATH) to the browser's xterm.js. Gated by FELIX_CLI_ENABLED. See
        `terminal.py` for the wire protocol and the security note."""
        from .terminal import run_terminal

        await run_terminal(ws)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, project: str = "sample", conn=Depends(db_conn)) -> dict:
        """A session's transcript + the diagnosis reconstructed from its linked
        episodic incident (CDC_INTERFACE §7.2). 404 if unknown. Pure read — no
        LLM guard, no recall re-run (`evidence` is null)."""
        from ..store.repositories import ActiveIncidentRepository, IncidentRepository

        session = ActiveIncidentRepository(conn, project).get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        incident = (
            IncidentRepository(conn, project).get(session.incident_id)
            if session.incident_id is not None
            else None
        )
        return session_to_dict(session, incident)

    @app.post("/chat")
    def chat(req: ChatRequest, project: str = "sample", conn=Depends(db_conn)) -> dict:
        """Full loop: recall -> reason -> write-back, scoped to `project`. Returns
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

        gatherer = EvidenceGatherer(conn, project=project)
        diagnoser = IncidentDiagnoser(
            gatherer,
            get_llm(),
            IncidentRepository(conn, project),
            ActionRepository(conn, project),
            ActiveIncidentRepository(conn, project),
        )
        result = diagnoser.respond(
            req.alert, origin_node=req.origin_node, k=req.k, session_id=req.session_id
        )
        return result_to_dict(result)

    @app.post("/chat/stream")
    def chat_stream(req: ChatRequest, project: str = "sample", conn=Depends(db_conn)) -> StreamingResponse:
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

        gatherer = EvidenceGatherer(conn, project=project)
        diagnoser = IncidentDiagnoser(
            gatherer,
            get_llm(),
            IncidentRepository(conn, project),
            ActionRepository(conn, project),
            ActiveIncidentRepository(conn, project),
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

    # Serve the built React UI at / so the deployed image answers the URL judges
    # open (the API + the SPA on one origin — see frontend/.env.production). The
    # Dockerfile copies the Vite build to ./frontend/dist; mounting LAST means
    # the explicit API routes above win, and this catches everything else.
    # html=True serves index.html for / and the SPA fallback. Guarded: a local
    # dev checkout without a build just skips the mount (the API still runs).
    dist = os.path.join(os.getcwd(), "frontend", "dist")
    if os.path.isdir(dist):
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    else:
        log.info("frontend/dist not found (%s) — serving API only, no UI", dist)

    return app


app = create_app()
