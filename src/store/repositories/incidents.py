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
                    (id, project, title, symptoms, root_cause, service, severity, tags, occurred_at, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
                """,
                (
                    id,
                    self.project,
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
        root_cause: str | None = None,
        service: str | None = None,
        severity: str | None = None,
    ) -> str:
        """Insert one incidents row WITHOUT an embedding (embedding stays NULL,
        so this row is invisible to vector recall — both `recall()` and
        `search()` filter `WHERE embedding IS NOT NULL`, so a NULL-embedding row
        is skipped entirely, not ranked. This matters: `embedding <-> query` is
        NULL for such a row, so returning it would crash the `float(distance)`
        parse — the filter, not ORDER BY, is what keeps it out).

        Used by the reasoning layer to create a parent incident row for an
        alert being diagnosed live, so resolution_steps have somewhere to
        attach. `root_cause` is persisted so `get()` (hence GET /sessions) can
        reconstruct the diagnosis — the CDC alert's only delivery channel to the
        UI. `id` is DB-generated (gen_random_uuid() default) since there's no
        deterministic seed id for a live alert. Returns the new incident id.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (project, title, symptoms, root_cause, service, severity)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (self.project, title, symptoms, root_cause, service, severity),
            )
            row = cur.fetchone()
        return str(row[0])

    def record_feedback(
        self, incident_id: str, *, helpful: bool, embedding: Sequence[float] | None = None
    ) -> bool:
        """Apply human feedback to a live-diagnosed incident — felix's learning
        signal (see schema.sql `incidents.feedback`).

        `helpful=True`: mark it 'helpful' AND set its embedding, promoting the
        row into recallable episodic memory (a live diagnosis is written with a
        NULL embedding, so this is what makes it findable by future alerts).
        `helpful=False`: mark it 'not_helpful' and clear the embedding again, so
        a diagnosis judged wrong is never recalled. Idempotent — re-marking just
        overwrites. Returns True if the incident existed (a row was updated).
        """
        with self.conn.cursor() as cur:
            if helpful:
                # embedding is required to promote; the endpoint always supplies it.
                cur.execute(
                    """
                    UPDATE incidents
                    SET feedback = 'helpful', embedding = %s::VECTOR(1024)
                    WHERE id = %s AND project = %s
                    """,
                    (
                        vec_literal(embedding) if embedding is not None else None,
                        incident_id,
                        self.project,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE incidents
                    SET feedback = 'not_helpful', embedding = NULL
                    WHERE id = %s AND project = %s
                    """,
                    (incident_id, self.project),
                )
            return cur.rowcount > 0

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

    def get(self, incident_id: str) -> Incident | None:
        """Load one incident by id with its ordered resolution_steps, or None if
        unknown. Used by GET /sessions to reconstruct the diagnosis for a
        CDC-minted session from its linked episodic row (no embedding needed)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, symptoms, root_cause, service, severity, feedback
                FROM incidents
                WHERE id = %s AND project = %s
                """,
                (incident_id, self.project),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                SELECT step_order, action, command, outcome
                FROM resolution_steps
                WHERE incident_id = %s
                ORDER BY step_order
                """,
                (incident_id,),
            )
            step_rows = cur.fetchall()
        return Incident(
            id=str(row[0]),
            title=row[1],
            symptoms=row[2],
            root_cause=row[3],
            service=row[4],
            severity=row[5],
            feedback=row[6],
            resolution_steps=[
                ResolutionStep(step_order=int(s[0]), action=s[1], command=s[2], outcome=s[3])
                for s in step_rows
            ],
        )

    @staticmethod
    def _row_to_incident(r) -> Incident:
        """Build an Incident from a (id, title, symptoms, root_cause, service,
        severity, tags, occurred_at, feedback) row — the column order shared by
        list_all() and search(). resolution_steps are attached separately (batch)."""
        return Incident(
            id=str(r[0]),
            title=r[1],
            symptoms=r[2],
            root_cause=r[3],
            service=r[4],
            severity=r[5],
            tags=list(r[6]) if r[6] else [],
            occurred_at=r[7],
            feedback=r[8],
        )

    def _attach_steps(self, incidents: list[Incident]) -> None:
        """Batch-load resolution_steps for many incidents in one query and attach
        them in place — avoids an N+1 when hydrating a whole list/search result."""
        if not incidents:
            return
        ids = [i.id for i in incidents]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT incident_id, step_order, action, command, outcome
                FROM resolution_steps
                WHERE incident_id = ANY(%s)
                ORDER BY incident_id, step_order
                """,
                (ids,),
            )
            rows = cur.fetchall()
        by_id: dict[str, list[ResolutionStep]] = {}
        for r in rows:
            by_id.setdefault(str(r[0]), []).append(
                ResolutionStep(step_order=int(r[1]), action=r[2], command=r[3], outcome=r[4])
            )
        for inc in incidents:
            inc.resolution_steps = by_id.get(inc.id, [])

    def list_all(self, limit: int = 200) -> list[Incident]:
        """All incidents (newest first), hydrated with their resolution_steps —
        the "browse the whole library" read behind GET /incidents. Ordered by
        when the incident occurred (live-minted rows with a NULL occurred_at sort
        last). Not vector-ranked: this is the un-searched, scroll-through view."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, symptoms, root_cause, service, severity, tags, occurred_at, feedback
                FROM incidents
                WHERE project = %s
                ORDER BY occurred_at DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (self.project, limit),
            )
            rows = cur.fetchall()
        incidents = [self._row_to_incident(r) for r in rows]
        self._attach_steps(incidents)
        return incidents

    def search(self, query_vec: Sequence[float], k: int = 10) -> list[Recall[Incident]]:
        """Semantic search over the incident library: top-k nearest to query_vec
        by L2 distance on the embedding, hydrated with resolution_steps. This is
        the showcase of CockroachDB's VECTOR search behind GET /incidents/search
        — richer than recall() (returns tags/occurred_at/steps for the browse
        cards) and it skips embedding-less rows (live-minted incidents)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, symptoms, root_cause, service, severity, tags, occurred_at, feedback,
                       embedding <-> %s::VECTOR(1024) AS distance
                FROM incidents
                WHERE embedding IS NOT NULL AND project = %s
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), self.project, k),
            )
            rows = cur.fetchall()
        recalls = [Recall(item=self._row_to_incident(r), distance=float(r[9])) for r in rows]
        self._attach_steps([rc.item for rc in recalls])
        return recalls

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
                WHERE embedding IS NOT NULL AND project = %s
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), self.project, k),
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
