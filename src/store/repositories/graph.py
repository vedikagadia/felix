"""GraphRepository — code_nodes / code_edges traversals.

code_edges are directed in the CALL direction: an edge (src -> dst) means `src`
calls / imports / depends on `dst`. That gives two traversals from a node:

  DOWNSTREAM (follow src -> dst): "what does this node reach?" — the blast
    radius / impact set. Use when a node is the *suspected cause* and you want
    everything it could break.

  UPSTREAM   (follow dst -> src): "who reaches this node?" — the callers up the
    stack. Use when a *symptom* is observed low in the stack (e.g.
    `db.pool.exhausted` surfaces at ConnectionPool.acquire) and you need to walk
    up toward where the root cause actually lives (CheckoutHandler.process). A
    log line shows where a failure *manifested*; this shows who *drove* it there.
"""

from __future__ import annotations

from ...models import CodeNode, GraphHit
from .base import BaseRepository


class GraphRepository(BaseRepository):
    def upsert_node(
        self,
        *,
        id: str,
        name: str,
        kind: str,
        file: str | None,
        service: str | None,
        source: str | None,
        summary: str | None,
        last_commit: str | None,
    ) -> str:
        """Upsert one code_nodes row. `id` is the caller-supplied deterministic
        uuid5 (service:file:kind:qualified_name) — code_nodes.id has no DEFAULT,
        the sync owns id generation so re-syncs UPSERT instead of duplicating."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPSERT INTO code_nodes
                    (id, project, name, kind, file, service, source, summary, last_commit)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (id, self.project, name, kind, file, service, source, summary, last_commit),
            )
        return id

    def upsert_edge(self, *, src_id: str, dst_id: str, kind: str) -> None:
        """Upsert one code_edges row ((src_id, dst_id, kind) is the PK)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPSERT INTO code_edges (src_id, dst_id, kind)
                VALUES (%s, %s, %s)
                """,
                (src_id, dst_id, kind),
            )

    def _traverse(self, start_name: str, max_depth: int, *, upstream: bool) -> list[GraphHit]:
        """Shared WITH RECURSIVE walk over code_edges from the node(s) named
        `start_name`, out to `max_depth` hops, in the given direction.

        Returns GraphHits ordered by depth (0 = the start node itself),
        deduplicated to the shallowest depth each node is reached at.
        """
        # Only the join flips between the two directions.
        step = (
            "SELECT e.src_id, r.depth + 1 FROM code_edges e JOIN reach r ON e.dst_id = r.id"
            if upstream
            else "SELECT e.dst_id, r.depth + 1 FROM code_edges e JOIN reach r ON e.src_id = r.id"
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE reach(id, depth) AS (
                    SELECT id, 0 FROM code_nodes WHERE name = %s AND project = %s
                    UNION ALL
                    {step}
                    WHERE r.depth < %s
                )
                SELECT MIN(r.depth) AS depth, n.*
                FROM reach r
                JOIN code_nodes n ON n.id = r.id
                GROUP BY n.id, n.name, n.kind, n.file, n.service, n.source,
                         n.summary, n.last_commit, n.updated_at
                ORDER BY depth
                """,
                (start_name, self.project, max_depth),
            )
            rows = cur.fetchall()
        # row: (depth, id, name, kind, file, service, source, summary, last_commit, updated_at)
        return [
            GraphHit(
                node=CodeNode(
                    id=str(r[1]),
                    name=r[2],
                    kind=r[3],
                    file=r[4],
                    service=r[5],
                    source=r[6],
                    summary=r[7],
                    last_commit=r[8],
                ),
                depth=int(r[0]),
            )
            for r in rows
        ]

    @staticmethod
    def _name_candidates(name: str) -> list[str]:
        """Generate normalized lookup candidates for `name`, most-specific
        first: the input as given (minus a `service:`-style prefix), then
        progressively dropping the leftmost dotted segment. E.g.
        "checkout_service.ConnectionPool.acquire" ->
        ["checkout_service.ConnectionPool.acquire", "ConnectionPool.acquire",
        "acquire"]. Matching is done case-insensitively by the caller."""
        stripped = name.split(":")[-1]
        segments = stripped.split(".")
        return [".".join(segments[i:]) for i in range(len(segments))]

    def find_node_by_name(self, name: str) -> CodeNode | None:
        """Look up one code_nodes row by name — exact match first, then a
        normalized match (case-insensitive; tolerant of a dotted `Class.method`
        name being matched against just `method`, or vice versa; strips a
        `service:`-style prefix if present).

        Used for origin-node resolution: candidate tokens pulled from recalled
        incident/doc text are logical component names, not always exact
        code-symbol names, so this is best-effort. Tries progressively less
        specific candidates (see `_name_candidates`) via exact/case-insensitive
        match before falling back to a trailing-segment suffix match (e.g.
        "acquire" -> "ConnectionPool.acquire"), preferring the shortest
        (most direct) name on ties so results are deterministic. Returns None
        if nothing matches — callers should degrade gracefully (skip the
        trace), not raise.
        """
        with self.conn.cursor() as cur:
            row = None
            for candidate in self._name_candidates(name):
                cur.execute(
                    """
                    SELECT id, name, kind, file, service, source, summary, last_commit
                    FROM code_nodes
                    WHERE lower(name) = lower(%s) AND project = %s
                    LIMIT 1
                    """,
                    (candidate, self.project),
                )
                row = cur.fetchone()
                if row is not None:
                    break
            if row is None:
                trailing = self._name_candidates(name)[-1]
                cur.execute(
                    """
                    SELECT id, name, kind, file, service, source, summary, last_commit
                    FROM code_nodes
                    WHERE lower(name) LIKE lower(%s) AND project = %s
                    ORDER BY length(name) ASC
                    LIMIT 1
                    """,
                    (f"%.{trailing}", self.project),
                )
                row = cur.fetchone()
            if row is None:
                return None
        return CodeNode(
            id=str(row[0]),
            name=row[1],
            kind=row[2],
            file=row[3],
            service=row[4],
            source=row[5],
            summary=row[6],
            last_commit=row[7],
        )

    def blast_radius(self, failing_name: str, max_depth: int = 3) -> list[GraphHit]:
        """DOWNSTREAM impact set: everything `failing_name` reaches by
        calling/importing, out to max_depth hops. Use when `failing_name` is the
        suspected *cause* and you want its blast radius."""
        return self._traverse(failing_name, max_depth, upstream=False)

    def upstream_callers(self, symptom_name: str, max_depth: int = 4) -> list[GraphHit]:
        """UPSTREAM origin trace: everything that reaches `symptom_name` (its
        callers, and their callers, …), out to max_depth hops.

        The "metric fired low in the stack, but where did it originate?" query.
        Start where the symptom surfaced (e.g. ConnectionPool.acquire for
        `db.pool.exhausted`) and walk up toward the root cause. Deeper depth =
        further up the stack = more likely the true origin."""
        return self._traverse(symptom_name, max_depth, upstream=True)
