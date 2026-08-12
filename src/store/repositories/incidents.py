"""IncidentRepository — incidents + their ordered resolution_steps."""

from __future__ import annotations

from typing import Sequence

import psycopg

from ...models import Incident, Recall, ResolutionStep
from ..connection import vec_literal
from .base import BaseRepository


class IncidentRepository(BaseRepository):
    @staticmethod
    def _insert_resolution_step(
        cur: psycopg.Cursor,
        incident_id: str,
        *,
        step_order: int,
        action: str,
        command: str | None,
        outcome: str | None,
    ) -> None:
        """Insert one resolution_steps row. Shared by insert() and
        add_resolution_steps() so the SQL lives in exactly one place."""
        cur.execute(
            """
            INSERT INTO resolution_steps
                (incident_id, step_order, action, command, outcome)
            VALUES
                (%s, %s, %s, %s, %s)
            """,
            (incident_id, step_order, action, command, outcome),
        )

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
                self._insert_resolution_step(
                    cur,
                    id,
                    step_order=step["step_order"],
                    action=step["action"],
                    command=step.get("command"),
                    outcome=step.get("outcome"),
                )
        return id

    def insert_minimal(
        self,
        *,
        title: str,
        symptoms: str,
        service: str | None = None,
        severity: str | None = None,
    ) -> str:
        """Insert one incidents row WITHOUT an embedding (embedding stays NULL,
        so this row is invisible to vector recall — `recall()`'s ORDER BY
        distance treats NULL embeddings as non-matching / sorts them last).

        Used by the reasoning layer to create a parent incident row for an
        alert being diagnosed live, so resolution_steps have somewhere to
        attach. `id` is DB-generated (gen_random_uuid() default) since there's
        no deterministic seed id for a live alert. Returns the new incident id.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (title, symptoms, service, severity)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (title, symptoms, service, severity),
            )
            row = cur.fetchone()
        return str(row[0])

    def add_resolution_steps(self, incident_id: str, steps: list[ResolutionStep]) -> None:
        """Insert ordered resolution_steps for an existing incident."""
        with self.conn.cursor() as cur:
            for step in steps:
                self._insert_resolution_step(
                    cur,
                    incident_id,
                    step_order=step.step_order,
                    action=step.action,
                    command=step.command,
                    outcome=step.outcome,
                )

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
