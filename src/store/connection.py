"""CockroachDB connection + the VECTOR param helper (psycopg 3).

Nothing here opens a connection at import time. `get_conn()` is the only thing
that touches the network; repositories take a connection explicitly so callers
control transaction/connection lifetime.

── Passing a VECTOR param via psycopg3 + CockroachDB ────────────────────────
CockroachDB's VECTOR type has no native psycopg3 Python-list adapter (that's
pgvector-python, which targets pgvector-on-Postgres, not CockroachDB's own
VECTOR). The approach used everywhere: format the Python list of floats as a
pgvector-style bracketed string literal — "[0.1,0.2,...]" — pass that STRING as
a normal bound parameter, then cast it to VECTOR(1024) in SQL with
`%s::VECTOR(1024)`. CockroachDB parses that bracketed form, the driver only
marshals a str (no manual interpolation / no injection risk), and it stays a
bound parameter typed as text. See `vec_literal()`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import psycopg

from ..config import get_settings


def get_conn() -> psycopg.Connection:
    """Open a new psycopg3 connection using DATABASE_URL from Settings.

    Autocommit is on by default (simplest for a CLI/agent issuing one statement
    or a short helper-driven sequence at a time). Callers needing a transaction
    wrap explicitly with `with conn.transaction():`.
    """
    conn = psycopg.connect(get_settings().database_url, autocommit=True)
    return conn


def apply_schema(conn: psycopg.Connection, path: str = "sql/schema.sql") -> None:
    """Execute the schema file (idempotent — schema.sql uses CREATE TABLE IF NOT EXISTS)."""
    sql_text = Path(path).read_text()
    with conn.cursor() as cur:
        cur.execute(sql_text)


def vec_literal(vec: Sequence[float]) -> str:
    """Format a Python vector as the pgvector-style bracketed string CockroachDB's
    VECTOR type parses: "[0.1,0.2,...]". Pass the result as a normal string bind
    param and cast with `::VECTOR(1024)` in the SQL text (see module docstring)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
