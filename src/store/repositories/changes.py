"""ChangeRepository — code_changes (the "what changed?" signal)."""

from __future__ import annotations

from typing import Sequence

from ...models import CodeChange, Recall
from ..connection import vec_literal
from .base import BaseRepository


class ChangeRepository(BaseRepository):
    def insert(
        self,
        *,
        id: str,
        commit_sha: str,
        merged_at: str,
        author: str | None,
        title: str,
        summary: str | None,
        files_changed: list[str] | None,
        services_affected: list[str] | None,
        affected_components: list[str] | None,
        embedding: Sequence[float],
    ) -> str:
        """Insert one code_changes row. `merged_at` accepts anything psycopg /
        CockroachDB can parse as TIMESTAMPTZ (ISO 8601 string or datetime)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO code_changes
                    (id, project, commit_sha, merged_at, author, title, summary,
                     files_changed, services_affected, affected_components, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
                """,
                (
                    id,
                    self.project,
                    commit_sha,
                    merged_at,
                    author,
                    title,
                    summary,
                    files_changed,
                    services_affected,
                    affected_components,
                    vec_literal(embedding),
                ),
            )
        return id

    def recall(
        self, query_vec: Sequence[float], k: int = 5, since_days: int = 14
    ) -> list[Recall[CodeChange]]:
        """Top-k code_changes nearest to query_vec, restricted to merges within
        the last `since_days` days (semantic AND temporal recall)."""
        with self.conn.cursor() as cur:
            # NOTE: psycopg's placeholder scanner skips %s inside quoted string
            # literals, so `interval '%s days'` would NOT get substituted — bind
            # since_days as an int and multiply a literal 1-day interval.
            cur.execute(
                """
                SELECT id, commit_sha, merged_at, title, summary,
                       embedding <-> %s::VECTOR(1024) AS distance
                FROM code_changes
                WHERE embedding IS NOT NULL
                  AND merged_at > now() - (%s * interval '1 day') AND project = %s
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), since_days, self.project, k),
            )
            rows = cur.fetchall()
        return [
            Recall(
                item=CodeChange(
                    id=str(r[0]),
                    commit_sha=r[1],
                    merged_at=r[2],
                    title=r[3],
                    summary=r[4],
                ),
                distance=float(r[5]),
            )
            for r in rows
        ]
