"""MetricRepository — live service telemetry (the CDC source table).

The sample checkout service writes one row per emitted sample; the watcher holds
a sinkless CHANGEFEED on this table. Transient operational data, never recalled
semantically — so no vector column and no domain model, just bare floats out.
"""

from __future__ import annotations

import json

from .base import BaseRepository


class MetricRepository(BaseRepository):
    def record(
        self, *, service: str, metric: str, value: float, labels: dict | None = None
    ) -> None:
        """INSERT one metrics row. labels is json.dumps'd (or NULL if None)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metrics (service, metric, value, labels)
                VALUES (%s, %s, %s, %s)
                """,
                (service, metric, value, json.dumps(labels) if labels is not None else None),
            )

    def recent(self, service: str, metric: str, limit: int = 200) -> list[float]:
        """Most-recent `limit` values for (service, metric), NEWEST-FIRST.

        Returns bare floats (the watcher only needs the distribution). Used only
        for a cold-start backfill of the in-memory window; the steady-state
        window is fed by the changefeed, not by polling this."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT value
                FROM metrics
                WHERE service = %s AND metric = %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (service, metric, limit),
            )
            rows = cur.fetchall()
        return [float(r[0]) for r in rows]
