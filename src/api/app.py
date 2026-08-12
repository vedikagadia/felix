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

import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import get_settings
from ..service.evidence_gatherer import EvidenceGatherer
from ..store.connection import get_conn
from .schemas import packet_to_dict, result_to_dict


class ChatRequest(BaseModel):
    alert: str = Field(..., min_length=1, description="The alert / error message text.")
    origin_node: str | None = Field(
        default=None,
        description="code_nodes.name where the symptom surfaces; enables the upstream graph trace.",
    )
    k: int = Field(default=3, ge=1, le=20, description="Results per memory source.")


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
        from ..store.repositories import ActionRepository, IncidentRepository

        gatherer = EvidenceGatherer(conn)
        diagnoser = IncidentDiagnoser(
            gatherer, get_llm(), IncidentRepository(conn), ActionRepository(conn)
        )
        result = diagnoser.respond(req.alert, origin_node=req.origin_node, k=req.k)
        return result_to_dict(result)

    return app


app = create_app()
