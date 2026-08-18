"""TopologyRepository — service_nodes / service_edges traversals.

The SERVICE-level topology graph (Layer 2), one step coarser than the
code-symbol graph in GraphRepository: nodes are whole services
(checkout-service, payment-gateway, …) and edges are `depends_on` relationships
directed src -> dst (`src` depends on `dst`). This is what a live health breach
is correlated against — "what does the breaching service reach downstream?".

The recursive walk mirrors GraphRepository._traverse (the canonical WITH
RECURSIVE pattern over a directed edge table) — reused here against
service_edges rather than re-derived; see graph.py for the annotated original.
Only the seed table (service_nodes/service_edges) and the projected columns
differ.
"""

from __future__ import annotations

import json

from ...models import CodeNode, GraphHit
from .base import BaseRepository


class TopologyRepository(BaseRepository):
    # ── UPSERT idiom (the loader's seam, mirroring GraphRepository) ───────────

    def upsert_node(
        self,
        *,
        id: str,
        name: str,
        kind: str,
        summary: str | None,
        health_checks: list[dict] | None,
    ) -> str:
        """Upsert one service_nodes row. `id` is the caller-supplied deterministic
        uuid5 (of the service name) — service_nodes.id has no DEFAULT, the seeder
        owns id generation so re-syncs UPSERT instead of duplicating. health_checks
        is json.dumps'd into the JSONB column (defaults to an empty array)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPSERT INTO service_nodes
                    (id, name, kind, summary, health_checks)
                VALUES
                    (%s, %s, %s, %s, %s)
                """,
                (id, name, kind, summary, json.dumps(health_checks or [])),
            )
        return id

    def upsert_edge(self, *, src_id: str, dst_id: str, kind: str) -> None:
        """Upsert one service_edges row ((src_id, dst_id, kind) is the PK)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPSERT INTO service_edges (src_id, dst_id, kind)
                VALUES (%s, %s, %s)
                """,
                (src_id, dst_id, kind),
            )

    # ── name resolution + reads ───────────────────────────────────────────────

    def _resolve_id(self, service: str) -> str | None:
        """Resolve a service name to its service_nodes id — exact match first,
        then case-insensitive. Returns None if unknown so callers degrade to []
        (mirrors GraphRepository.find_node_by_name's graceful-miss contract, but
        over service_nodes: a code_nodes id would not exist in service_edges)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM service_nodes
                WHERE name = %s OR lower(name) = lower(%s)
                ORDER BY (name = %s) DESC, length(name) ASC
                LIMIT 1
                """,
                (service, service, service),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    def all_names(self) -> list[str]:
        """Every known service_nodes.name — the vocabulary the alert text is
        matched against when extracting which service an alert is about."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT name FROM service_nodes")
            rows = cur.fetchall()
        return [r[0] for r in rows]

    def health_checks_for(self, services: list[str]) -> dict[str, list[dict]]:
        """Batch-read the health_checks arrays for many service names in one
        query, keyed by name. psycopg returns the JSONB already-decoded
        (list[dict]) — no json.loads (mirrors metrics.labels). Names with no row
        (or a NULL/empty array) are simply absent from the result; the caller
        treats a missing key as []."""
        if not services:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, health_checks
                FROM service_nodes
                WHERE name = ANY(%s)
                """,
                (list(services),),
            )
            rows = cur.fetchall()
        return {r[0]: (r[1] or []) for r in rows}

    def downstream_dependencies(self, service: str, max_depth: int = 3) -> list[GraphHit]:
        """DOWNSTREAM dependency set for a service node: everything `service`
        reaches via `depends_on` edges, out to max_depth hops (0 = the service
        itself). Semantics identical to GraphRepository.blast_radius, but over
        the service-topology graph rather than the code graph.

        Returns GraphHits ordered by depth, deduplicated to the shallowest depth
        each node is reached at; [] if the name doesn't resolve.
        """
        node_id = self._resolve_id(service)
        if node_id is None:
            return []
        with self.conn.cursor() as cur:
            # Mirrors GraphRepository._traverse's WITH RECURSIVE walk in the
            # DOWNSTREAM direction (follow src -> dst) against service_edges.
            # health_checks (JSONB) is deliberately not projected — a GraphHit is
            # node identity + depth; per-node checks are read via health_checks_for.
            cur.execute(
                """
                WITH RECURSIVE reach(id, depth) AS (
                    SELECT id, 0 FROM service_nodes WHERE id = %s
                    UNION ALL
                    SELECT e.dst_id, r.depth + 1
                    FROM service_edges e JOIN reach r ON e.src_id = r.id
                    WHERE r.depth < %s
                )
                SELECT MIN(r.depth) AS depth, n.id, n.name, n.kind, n.summary
                FROM reach r
                JOIN service_nodes n ON n.id = r.id
                GROUP BY n.id, n.name, n.kind, n.summary
                ORDER BY depth
                """,
                (node_id, max_depth),
            )
            rows = cur.fetchall()
        # row: (depth, id, name, kind, summary)
        return [
            GraphHit(
                node=CodeNode(
                    id=str(r[1]),
                    name=r[2],
                    kind=r[3],
                    service=r[2],
                    summary=r[4],
                ),
                depth=int(r[0]),
            )
            for r in rows
        ]
