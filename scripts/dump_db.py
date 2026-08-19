"""Regenerate sql/seed_dump.sql — a portable, data-only snapshot of felix.

    python -m scripts.dump_db                 # -> sql/seed_dump.sql
    python -m scripts.dump_db --out /tmp/x.sql

Why this exists: the Docker bring-up (docker/init-db.sh, driven by
docker-compose) seeds a fresh CockroachDB node from sql/seed_dump.sql instead of
running the Python seeder, because the minimal `cockroachdb/cockroach` image has
no Python and no embedding model. So the dump carries the *precomputed*
embeddings — regenerating it re-embeds nothing, it just serializes whatever is
currently in the DB. `cockroach dump` was removed in modern CockroachDB, so we
emit the INSERTs ourselves.

Usage: seed a DB the normal way first (`python -m src seed --apply-schema`,
which populates all sources incl. runbooks + topology with real embeddings),
then run this to capture it. Reads DATABASE_URL from .env like the rest of felix.

The output is data-only — run sql/schema.sql first to create the tables. Tables
are emitted in FK-safe order (parents before children). Only the seedable memory
sources are dumped; runtime tables (agent_actions, metrics, active_incidents,
projects) are intentionally skipped — schema.sql seeds the one 'sample' projects
row, and the rest fill at runtime.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from src.store.connection import get_conn

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "sql" / "seed_dump.sql"

# FK-safe order: parents before the child tables that reference them
# (resolution_steps -> incidents, runbook_steps -> runbooks, *_edges -> *_nodes).
TABLES = [
    "incidents",
    "resolution_steps",
    "doc_chunks",
    "code_nodes",
    "code_edges",
    "code_changes",
    "runbooks",
    "runbook_steps",
    "service_nodes",
    "service_edges",
]


def _sql_str(s: str) -> str:
    """Single-quote a string literal, doubling embedded quotes."""
    return "'" + s.replace("'", "''") + "'"


def _sql_array(items: list) -> str:
    """A CockroachDB STRING[] literal: '{"a","b"}' with element quotes escaped
    (backslash-escaped inside the double-quoted element, per array-literal rules)."""
    parts = []
    for it in items:
        parts.append('"' + str(it).replace("\\", "\\\\").replace('"', '\\"') + '"')
    return _sql_str("{" + ",".join(parts) + "}")


def _fmt(val) -> str:
    """Render one Python value as a CockroachDB SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, dt.datetime):
        return _sql_str(str(val))
    if isinstance(val, list):
        # jsonb (list-of-dicts / nested) vs STRING[] (list of scalars).
        if val and isinstance(val[0], (dict, list)):
            return _sql_str(json.dumps(val))
        return _sql_array(val)
    if isinstance(val, dict):
        return _sql_str(json.dumps(val))
    # strings — includes the vector column, already '[...]' text from CRDB
    return _sql_str(str(val))


def dump(out_path: Path) -> dict[str, int]:
    conn = get_conn()
    counts: dict[str, int] = {}
    try:
        lines: list[str] = [
            "-- felix data dump (data only; run sql/schema.sql first).",
            "-- Regenerate with: python -m scripts.dump_db",
            "-- (seed a DB via `python -m src seed --apply-schema` first, then dump it).",
            "",
        ]
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_name = %s ORDER BY ordinal_position""",
                    (table,),
                )
                cols = [r[0] for r in cur.fetchall()]
                collist = ", ".join(cols)
                cur.execute(f"SELECT {collist} FROM {table}")
                rows = cur.fetchall()
                counts[table] = len(rows)
                lines.append(f"-- {table}: {len(rows)} rows")
                for row in rows:
                    values = ", ".join(_fmt(v) for v in row)
                    lines.append(f"INSERT INTO {table} ({collist}) VALUES ({values});")
                lines.append("")
        out_path.write_text("\n".join(lines))
    finally:
        conn.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Regenerate sql/seed_dump.sql from the live DB.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output path (default sql/seed_dump.sql)")
    args = ap.parse_args()

    out = Path(args.out)
    counts = dump(out)
    total = sum(counts.values())
    print(f"wrote {out} ({total} rows):")
    for table, n in counts.items():
        print(f"  {table:<18} {n}")


if __name__ == "__main__":
    main()
