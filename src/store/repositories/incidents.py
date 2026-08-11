"""IncidentRepository — incidents + their ordered resolution_steps."""

from __future__ import annotations

from typing import Sequence

from ...models import Incident, Recall, ResolutionStep
from ..connection import vec_literal
from .base import BaseRepository


class IncidentRepository(BaseRepository):
    def insert(
        self,
        *,
        id: str,
        title: str,
        symptoms: str,
        root_cause: str | None,
        service: str | None,
        severity: str | None,
        tags: list[str] | None,
        embedding: Sequence[float],
        occurred_at: str | None = None,
        resolution_steps: list[dict] | None = None,
    ) -> str:
        """Insert one incidents row plus its ordered resolution_steps (if given).

        `id` is required (not defaulted) so seeders can pick deterministic ids the
        same way code_nodes does. `occurred_at` is when the incident happened
        (ISO 8601 string), distinct from the created_at record timestamp.
        `resolution_steps` is a list of {"step_order","action","command","outcome"}.
        Returns the incident id.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents
                    (id, title, symptoms, root_cause, service, severity, tags, occurred_at, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
                """,
                (
                    id,
                    title,
                    symptoms,
                    root_cause,
                    service,
                    severity,
                    tags,
                    occurred_at,
                    vec_literal(embedding),
                ),
            )
            for step in resolution_steps or []:
                cur.execute(
                    """
                    INSERT INTO resolution_steps
                        (incident_id, step_order, action, command, outcome)
                    VALUES
                        (%s, %s, %s, %s, %s)
                    """,
                    (
                        id,
                        step["step_order"],
                        step["action"],
                        step.get("command"),
                        step.get("outcome"),
                    ),
                )
        return id

    def recall(self, query_vec: Sequence[float], k: int = 5) -> list[Recall[Incident]]:
        """Top-k incidents nearest to query_vec, by L2 distance on embedding."""
        with self.conn.cursor() as cur:
            # NOTE: `<->` is L2 distance for CockroachDB VECTOR; swap to `<=>`
            # (cosine) if the cluster/index is built for cosine distance instead.
            cur.execute(
                """
                SELECT id, title, severity, symptoms, root_cause, service,
                       embedding <-> %s::VECTOR(1024) AS distance
                FROM incidents
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), k),
            )
            rows = cur.fetchall()
        return [
            Recall(
                item=Incident(
                    id=str(r[0]),
                    title=r[1],
                    severity=r[2],
                    symptoms=r[3],
                    root_cause=r[4],
                    service=r[5],
                ),
                distance=float(r[6]),
            )
            for r in rows
        ]
