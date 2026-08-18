"""RunbookRepository — runbooks + their ordered runbook_steps.

Runbooks are curated, reusable playbooks recalled by meaning: given an alert,
felix surfaces the runbook whose trigger text is nearest by vector distance and
presents its ordered steps. Mirrors IncidentRepository (a parent row + ordered
child steps, embedded on `title + symptoms`) — the difference is intent:
incidents are episodic history, runbooks are authored procedure.
"""

from __future__ import annotations

from typing import Sequence

import psycopg

from ...models import Recall, Runbook, RunbookStep
from ..connection import vec_literal
from .base import BaseRepository


class RunbookRepository(BaseRepository):
    @staticmethod
    def _insert_step(
        cur: psycopg.Cursor,
        runbook_id: str,
        *,
        step_order: int,
        action: str,
        command: str | None,
        outcome: str | None,
    ) -> None:
        """Insert one runbook_steps row (mirrors IncidentRepository's
        _insert_resolution_step, so the child-table SQL lives in one place)."""
        cur.execute(
            """
            INSERT INTO runbook_steps
                (runbook_id, step_order, action, command, outcome)
            VALUES
                (%s, %s, %s, %s, %s)
            """,
            (runbook_id, step_order, action, command, outcome),
        )

    def insert(
        self,
        *,
        id: str,
        title: str,
        symptoms: str,
        service: str | None,
        tags: list[str] | None,
        embedding: Sequence[float],
        steps: list[dict] | None = None,
    ) -> str:
        """Insert one runbooks row plus its ordered runbook_steps (if given).

        `id` is required (not defaulted) so the seeder can pick deterministic
        ids the same way incidents/code_nodes do. `steps` is a list of
        {"step_order","action","command","outcome"}. Returns the runbook id.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runbooks
                    (id, title, symptoms, service, tags, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s::VECTOR(1024))
                """,
                (id, title, symptoms, service, tags, vec_literal(embedding)),
            )
            for step in steps or []:
                self._insert_step(
                    cur,
                    id,
                    step_order=step["step_order"],
                    action=step["action"],
                    command=step.get("command"),
                    outcome=step.get("outcome"),
                )
        return id

    def _attach_steps(self, runbooks: list[Runbook]) -> None:
        """Batch-load runbook_steps for many runbooks in one query and attach
        them in place — avoids an N+1 when hydrating a recall result (mirrors
        IncidentRepository._attach_steps)."""
        if not runbooks:
            return
        ids = [r.id for r in runbooks]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT runbook_id, step_order, action, command, outcome
                FROM runbook_steps
                WHERE runbook_id = ANY(%s)
                ORDER BY runbook_id, step_order
                """,
                (ids,),
            )
            rows = cur.fetchall()
        by_id: dict[str, list[RunbookStep]] = {}
        for r in rows:
            by_id.setdefault(str(r[0]), []).append(
                RunbookStep(step_order=int(r[1]), action=r[2], command=r[3], outcome=r[4])
            )
        for rb in runbooks:
            rb.steps = by_id.get(rb.id, [])

    def recall(self, query_vec: Sequence[float], k: int = 5) -> list[Recall[Runbook]]:
        """Top-k runbooks nearest to query_vec by L2 distance on embedding,
        hydrated with their ordered runbook_steps. Mirrors
        IncidentRepository.recall/search: ORDER BY distance LIMIT k, skipping
        NULL-embedding rows."""
        with self.conn.cursor() as cur:
            # NOTE: `<->` is L2 distance for CockroachDB VECTOR; swap to `<=>`
            # (cosine) if the cluster/index is built for cosine distance instead.
            cur.execute(
                """
                SELECT id, title, symptoms, service, tags, created_at,
                       embedding <-> %s::VECTOR(1024) AS distance
                FROM runbooks
                WHERE embedding IS NOT NULL
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), k),
            )
            rows = cur.fetchall()
        recalls = [
            Recall(
                item=Runbook(
                    id=str(r[0]),
                    title=r[1],
                    symptoms=r[2],
                    service=r[3],
                    tags=list(r[4]) if r[4] else [],
                    created_at=r[5],
                ),
                distance=float(r[6]),
            )
            for r in rows
        ]
        self._attach_steps([rc.item for rc in recalls])
        return recalls
