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
from .schemas import diagnosis_to_dict, packet_to_dict, result_to_dict


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
                        yield _sse(
                            "done",
                            {
                                "diagnosis": diagnosis_to_dict(payload.diagnosis),
                                "evidence": packet_to_dict(payload.evidence),
                                "session_id": payload.session_id,
                            },
                        )
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
