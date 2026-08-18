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
                INSERT INTO metrics (project, service, metric, value, labels)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (self.project, service, metric, value, json.dumps(labels) if labels is not None else None),
            )

    def recent_samples(
        self,
        limit: int = 200,
        service: str | None = None,
        metric: str | None = None,
    ) -> list[dict]:
        """Recent metric rows (newest-first), optionally filtered by service
        and/or metric. Powers the live-monitoring panel's cold-start backfill:
        the panel seeds each series' sparkline from these before subscribing to
        the changefeed for steady-state samples. Returns dicts (service, metric,
        value, ts, labels) — `ts` a psycopg datetime the API layer ISO-8601s."""
        clauses: list[str] = ["project = %s"]
        params: list = [self.project]
        if service:
            clauses.append("service = %s")
            params.append(service)
        if metric:
            clauses.append("metric = %s")
            params.append(metric)
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT service, metric, value, ts, labels
                FROM metrics
                {where}
                ORDER BY ts DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [
            {"service": r[0], "metric": r[1], "value": float(r[2]), "ts": r[3], "labels": r[4]}
            for r in rows
        ]

    def recent_by_services(
        self,
        services: list[str],
        metric: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Recent metric rows (newest-first) for MANY services in one query —
        `service = ANY(%s)` with the list bound, not interpolated — optionally
        narrowed to a single metric. Powers MetricQueryBuilder.fetch's single
        batched read: one round trip that the caller buckets by (service,
        metric) in Python. Returns the same dict shape as recent_samples
        (service, metric, value, ts, labels). Empty `services` => [] with no
        query issued.

        `limit` is PER (service, metric), not a global cap: a window function
        ranks each series by ts DESC and keeps its own newest `limit`. A single
        global LIMIT would let a high-frequency service (checkout emits 3
        rows/iteration) consume the whole budget and starve a low-frequency
        dependency to zero rows — which the health sweep would then misread as
        "no data" and silently drop that dependency's real breach."""
        if not services:
            return []
        clauses: list[str] = ["project = %s", "service = ANY(%s)"]
        params: list = [self.project, list(services)]
        if metric:
            clauses.append("metric = %s")
            params.append(metric)
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT service, metric, value, ts, labels FROM (
                    SELECT service, metric, value, ts, labels,
                           row_number() OVER (
                               PARTITION BY service, metric ORDER BY ts DESC
                           ) AS rn
                    FROM metrics
                    {where}
                ) ranked
                WHERE rn <= %s
                ORDER BY ts DESC
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [
            {"service": r[0], "metric": r[1], "value": float(r[2]), "ts": r[3], "labels": r[4]}
            for r in rows
        ]

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
                WHERE project = %s AND service = %s AND metric = %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (self.project, service, metric, limit),
            )
            rows = cur.fetchall()
        return [float(r[0]) for r in rows]
