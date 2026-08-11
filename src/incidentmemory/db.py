"""CockroachDB connection + query helpers (psycopg 3).

Targets the schema in sql/schema.sql exactly: incidents, resolution_steps,
doc_chunks, code_nodes, code_edges, code_changes, agent_actions.

Nothing here opens a connection at import time. `get_conn()` is the only thing
that touches the network, and every helper below takes a connection explicitly
so callers control transaction/connection lifetime.

── Passing a VECTOR param via psycopg3 + CockroachDB ────────────────────────
CockroachDB's VECTOR type is not one psycopg3 has a native Python-list adapter
for (that's the pgvector-python package, which targets pgvector-on-Postgres,
not CockroachDB's own VECTOR implementation). The approach used everywhere in
this file: format the Python list of floats as a pgvector-style bracketed
string literal — "[0.1,0.2,...]" — and pass that STRING as a normal
parameterized value, then cast it to VECTOR(1024) on the SQL side with
`%s::VECTOR(1024)`. This works because CockroachDB parses that bracketed
string form for VECTOR literals, and the driver only ever has to marshal a
str, which psycopg3 already knows how to do safely (no manual string
interpolation / no injection risk — it's still a bound parameter, just typed
as text and cast in SQL).
See `_vec_literal()` below; every INSERT/recall helper uses it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv

load_dotenv()


# ── connection ───────────────────────────────────────────────────────────────


def get_conn() -> psycopg.Connection:
    """Open a new psycopg3 connection using DATABASE_URL from the environment.

    Autocommit is on by default (simplest for a CLI/agent that issues one
    statement or a short helper-driven sequence at a time). Callers that need
    a transaction can wrap explicitly with `with conn.transaction():`.
    """
    load_dotenv()  # re-load in case .env changed since process start (cheap, idempotent)
    database_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(database_url, autocommit=True)
    return conn


def apply_schema(conn: psycopg.Connection, path: str = "sql/schema.sql") -> None:
    """Execute the schema file (idempotent — schema.sql uses CREATE TABLE IF NOT EXISTS)."""
    sql_text = Path(path).read_text()
    with conn.cursor() as cur:
        cur.execute(sql_text)


# ── vector param helper ──────────────────────────────────────────────────────


def _vec_literal(vec: Sequence[float]) -> str:
    """Format a Python vector as the pgvector-style bracketed string CockroachDB's
    VECTOR type parses: "[0.1,0.2,...]". Pass the result as a normal string bind
    param and cast with `::VECTOR(1024)` in the SQL text (see module docstring)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ── insert helpers ───────────────────────────────────────────────────────────


def insert_incident(
    conn: psycopg.Connection,
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
    resolution_steps: list[dict[str, Any]] | None = None,
) -> str:
    """Insert one incidents row plus its ordered resolution_steps (if given).

    `id` is required (not defaulted) so seeders can pick deterministic ids the
    same way code_nodes does; pass a fresh UUID string if you don't care.
    `occurred_at` is when the incident happened (ISO 8601 string), distinct from
    the created_at record timestamp.
    `resolution_steps` is a list of {"step_order", "action", "command", "outcome"}.
    Returns the incident id.
    """
    with conn.cursor() as cur:
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
                _vec_literal(embedding),
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


def insert_doc_chunk(
    conn: psycopg.Connection,
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
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO doc_chunks
                (id, doc_title, heading, body, doc_type, source_path, embedding)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
            """,
            (id, doc_title, heading, body, doc_type, source_path, _vec_literal(embedding)),
        )
    return id


def insert_code_node(
    conn: psycopg.Connection,
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
    """Upsert one code_nodes row. `id` is the caller-supplied deterministic uuid5
    (service:file:kind:qualified_name) — code_nodes.id has no DEFAULT in the schema,
    the sync script owns id generation so re-syncs UPSERT instead of duplicating."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPSERT INTO code_nodes
                (id, name, kind, file, service, source, summary, last_commit)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (id, name, kind, file, service, source, summary, last_commit),
        )
    return id


def insert_code_edge(
    conn: psycopg.Connection,
    *,
    src_id: str,
    dst_id: str,
    kind: str,
) -> None:
    """Upsert one code_edges row (src_id, dst_id, kind) is the PK)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPSERT INTO code_edges (src_id, dst_id, kind)
            VALUES (%s, %s, %s)
            """,
            (src_id, dst_id, kind),
        )


def insert_code_change(
    conn: psycopg.Connection,
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
    """Insert one code_changes row. `merged_at` accepts anything psycopg/CockroachDB
    can parse as a TIMESTAMPTZ (ISO 8601 string or datetime)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO code_changes
                (id, commit_sha, merged_at, author, title, summary,
                 files_changed, services_affected, affected_components, embedding)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR(1024))
            """,
            (
                id,
                commit_sha,
                merged_at,
                author,
                title,
                summary,
                files_changed,
                services_affected,
                affected_components,
                _vec_literal(embedding),
            ),
        )
    return id


def log_action(
    conn: psycopg.Connection,
    *,
    action_type: str,
    tool_called: str | None = None,
    input: Any = None,
    output: Any = None,
    model: str | None = None,
    tokens: int | None = None,
) -> None:
    """Append one row to the agent_actions audit log.

    `input`/`output` are stored as JSONB; pass dicts/lists/primitives (they're
    json.dumps'd here) or a pre-serialized JSON string.
    """
    input_json = input if isinstance(input, str) or input is None else json.dumps(input)
    output_json = output if isinstance(output, str) or output is None else json.dumps(output)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_actions
                (action_type, tool_called, input, output, model, tokens)
            VALUES
                (%s, %s, %s, %s, %s, %s)
            """,
            (action_type, tool_called, input_json, output_json, model, tokens),
        )


# ── vector recall helpers ────────────────────────────────────────────────────
#
# Each runs an exact nearest-neighbor scan (ORDER BY embedding <-> %s LIMIT %s),
# per the schema's documented Basic-tier fallback (no vector index required at
# seed scale). Written against psycopg3 + a live CockroachDB cluster but not
# exercised yet — see the NOTE on the query line most likely to need tweaking.


def recall_incidents(conn: psycopg.Connection, query_vec: Sequence[float], k: int = 5):
    """Top-k incidents nearest to query_vec, by L2 distance on embedding.

    Returns rows: (id, title, severity, symptoms, root_cause, service, distance).
    """
    with conn.cursor() as cur:
        # NOTE: `<->` is L2 distance for CockroachDB VECTOR; swap to `<=>` (cosine)
        # here if the live cluster/index is built for cosine distance instead.
        cur.execute(
            """
            SELECT id, title, severity, symptoms, root_cause, service,
                   embedding <-> %s::VECTOR(1024) AS distance
            FROM incidents
            ORDER BY distance
            LIMIT %s
            """,
            (_vec_literal(query_vec), k),
        )
        return cur.fetchall()


def recall_docs(conn: psycopg.Connection, query_vec: Sequence[float], k: int = 5):
    """Top-k doc_chunks nearest to query_vec, by L2 distance on embedding.

    Returns rows: (id, doc_title, heading, body, doc_type, distance).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, doc_title, heading, body, doc_type,
                   embedding <-> %s::VECTOR(1024) AS distance
            FROM doc_chunks
            ORDER BY distance
            LIMIT %s
            """,
            (_vec_literal(query_vec), k),
        )
        return cur.fetchall()


def recall_changes(
    conn: psycopg.Connection,
    query_vec: Sequence[float],
    k: int = 5,
    since_days: int = 14,
):
    """Top-k code_changes nearest to query_vec, restricted to merges within the
    last `since_days` days (semantic AND temporal recall — see schema.sql).

    Returns rows: (id, commit_sha, merged_at, title, summary, distance).
    """
    with conn.cursor() as cur:
        # NOTE: psycopg's placeholder scanner skips %s inside quoted string literals,
        # so `interval '%s days'` would NOT get substituted — instead we bind
        # since_days as an int and multiply against a literal 1-day interval.
        cur.execute(
            """
            SELECT id, commit_sha, merged_at, title, summary,
                   embedding <-> %s::VECTOR(1024) AS distance
            FROM code_changes
            WHERE merged_at > now() - (%s * interval '1 day')
            ORDER BY distance
            LIMIT %s
            """,
            (_vec_literal(query_vec), since_days, k),
        )
        return cur.fetchall()


# ── graph traversal ──────────────────────────────────────────────────────────
#
# code_edges are directed in the CALL direction: an edge (src -> dst) means
# `src` calls / imports / depends on `dst`. That gives two very different, both
# useful, traversals from a starting node:
#
#   DOWNSTREAM (follow src -> dst): "what does this node reach?" — the blast
#     radius / impact set. Use when a node is the *suspected cause* and you want
#     everything it could break.
#
#   UPSTREAM   (follow dst -> src): "who reaches this node?" — the callers up
#     the stack. Use when you have a *symptom* observed low in the stack (e.g.
#     `db.pool.exhausted` surfaces at ConnectionPool.acquire) and need to walk
#     up toward where the root cause actually lives (CheckoutHandler.process).
#     A log line shows you where a failure *manifested*; this shows you who
#     *drove* it there.


def _traverse(conn: psycopg.Connection, start_name: str, max_depth: int, *, upstream: bool):
    """Shared WITH RECURSIVE walk over code_edges from the node(s) named
    `start_name`, out to `max_depth` hops, in the given direction.

    Returns rows (depth, id, name, kind, file, service, source, summary,
    last_commit, updated_at), ordered by depth (0 = the start node itself),
    deduplicated to the shallowest depth each node is reached at.
    """
    # Only the join flips between the two directions.
    step = (
        "SELECT e.src_id, r.depth + 1 FROM code_edges e JOIN reach r ON e.dst_id = r.id"
        if upstream
        else "SELECT e.dst_id, r.depth + 1 FROM code_edges e JOIN reach r ON e.src_id = r.id"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH RECURSIVE reach(id, depth) AS (
                SELECT id, 0 FROM code_nodes WHERE name = %s
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
            (start_name, max_depth),
        )
        return cur.fetchall()


def graph_blast_radius(conn: psycopg.Connection, failing_name: str, max_depth: int = 3):
    """DOWNSTREAM impact set: everything `failing_name` reaches by calling/importing,
    out to max_depth hops. Use when `failing_name` is the suspected *cause* and you
    want its blast radius. Returns (depth, code_nodes.*) ordered by depth."""
    return _traverse(conn, failing_name, max_depth, upstream=False)


def graph_upstream_callers(conn: psycopg.Connection, symptom_name: str, max_depth: int = 4):
    """UPSTREAM origin trace: everything that reaches `symptom_name` (its callers,
    and their callers, …), out to max_depth hops.

    This is the "the metric fired low in the stack, but where did it originate?"
    query. Start at the node where the symptom surfaced (e.g. ConnectionPool.acquire
    for `db.pool.exhausted`) and walk up the call graph toward the root cause.
    Deeper depth = further up the stack = more likely to be the true origin.
    Returns (depth, code_nodes.*) ordered by depth."""
    return _traverse(conn, symptom_name, max_depth, upstream=True)
