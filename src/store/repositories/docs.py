"""DocRepository — documentation chunks."""

from __future__ import annotations

from typing import Sequence

from ...models import DocChunk, Recall
from ..connection import vec_literal
from .base import BaseRepository


class DocRepository(BaseRepository):
    def insert(
        self,
        *,
        id: str,
        doc_title: str,
        heading: str | None,
        body: str,
        doc_type: str | None,
        embedding: Sequence[float],
        source_path: str | None = None,
    ) -> str:
        """Insert one doc_chunks row. Returns the chunk id."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO doc_chunks
                    (id, project, doc_title, heading, body, doc_type, source_path, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
                """,
                (id, self.project, doc_title, heading, body, doc_type, source_path, vec_literal(embedding)),
            )
        return id

    def recall(self, query_vec: Sequence[float], k: int = 5) -> list[Recall[DocChunk]]:
        """Top-k doc_chunks nearest to query_vec, by L2 distance on embedding."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, doc_title, heading, body, doc_type,
                       embedding <-> %s::VECTOR(1024) AS distance
                FROM doc_chunks
                WHERE project = %s
                ORDER BY distance
                LIMIT %s
                """,
                (vec_literal(query_vec), self.project, k),
            )
            rows = cur.fetchall()
        return [
            Recall(
                item=DocChunk(
                    id=str(r[0]),
                    doc_title=r[1],
                    heading=r[2],
                    body=r[3],
                    doc_type=r[4],
                ),
                distance=float(r[5]),
            )
            for r in rows
        ]
