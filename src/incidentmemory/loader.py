"""Seed loader — populate CockroachDB with felix's four memory sources.

Ties the pieces together end to end:
  1. parse the sample project into a code graph (parser.parse_project)
  2. load the three seed corpora (incidents / docs / code_changes JSON)
  3. embed the searchable text of each row (embeddings.embed — Titan or local)
  4. insert everything via the db.* helpers (which target sql/schema.sql)

Idempotent enough to re-run: code_nodes/code_edges UPSERT by deterministic id;
incidents/docs/code_changes use the seed's stable string ids as UUIDs so a
re-run would conflict — pass --truncate to clear those three tables first.

Nothing here touches the network at import time. Run it (needs DATABASE_URL and
a reachable cluster; EMBED_PROVIDER picks Titan vs the local stand-in):

    python -m src.incidentmemory.loader --apply-schema
    python -m src.incidentmemory.loader --truncate         # reseed from scratch

The embedding text conventions match the schema comments:
  incident   -> title + symptoms
  doc chunk  -> heading + body
  code change-> title + summary
Code nodes/edges are graph-traversed, not vector-searched, so they carry no
embedding in this phase.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from . import db
from . import embeddings
from .parser import parse_project

# repo-root-relative defaults (this file is src/incidentmemory/loader.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = REPO_ROOT / "sample_project"
SEED_DIR = SAMPLE_ROOT / "seed"

# Namespace to turn the seeds' stable string ids (e.g. "inc-0001") into stable
# UUIDs, so re-runs address the same rows and the ids stay human-traceable.
SEED_NS = uuid.UUID("f00dfeed-0000-0000-0000-000000000000")


def _seed_uuid(stable_id: str) -> str:
    return str(uuid.uuid5(SEED_NS, stable_id))


def _load_json(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return json.load(f)


# ── individual loaders ───────────────────────────────────────────────────────


def load_code_graph(conn) -> tuple[int, int]:
    """Parse the sample project and UPSERT its nodes + edges. No embeddings."""
    nodes, edges = parse_project(str(SAMPLE_ROOT))
    for n in nodes:
        db.insert_code_node(
            conn,
            id=n["id"],
            name=n["name"],
            kind=n["kind"],
            file=n["file"],
            service=n["service"],
            source=n["source"],
            summary=n["summary"],
            last_commit=n["last_commit"],
        )
    for e in edges:
        db.insert_code_edge(conn, src_id=e["src_id"], dst_id=e["dst_id"], kind=e["kind"])
    return len(nodes), len(edges)


def load_incidents(conn, path: Path | None = None) -> int:
    rows = _load_json(path or SEED_DIR / "incidents.json")
    for r in rows:
        text = f"{r['title']}\n{r['symptoms']}"
        db.insert_incident(
            conn,
            id=_seed_uuid(r["id"]),
            title=r["title"],
            symptoms=r["symptoms"],
            root_cause=r.get("root_cause"),
            service=r.get("service"),
            severity=r.get("severity"),
            tags=r.get("tags"),
            occurred_at=r.get("occurred_at"),
            embedding=embeddings.embed(text),
            resolution_steps=r.get("resolution_steps"),
        )
    return len(rows)


def load_docs(conn, path: Path | None = None) -> int:
    rows = _load_json(path or SEED_DIR / "docs.json")
    for r in rows:
        text = f"{r.get('heading', '')}\n{r['body']}"
        db.insert_doc_chunk(
            conn,
            id=_seed_uuid(r["id"]),
            doc_title=r["doc_title"],
            heading=r.get("heading"),
            body=r["body"],
            doc_type=r.get("doc_type"),
            source_path=r.get("source_path"),
            embedding=embeddings.embed(text),
        )
    return len(rows)


def load_code_changes(conn, path: Path | None = None) -> int:
    rows = _load_json(path or SEED_DIR / "code_changes.json")
    for r in rows:
        text = f"{r['title']}\n{r.get('summary', '')}"
        db.insert_code_change(
            conn,
            id=_seed_uuid(r["id"]),
            commit_sha=r["commit_sha"],
            merged_at=r["merged_at"],
            author=r.get("author"),
            title=r["title"],
            summary=r.get("summary"),
            files_changed=r.get("files_changed"),
            services_affected=r.get("services_affected"),
            affected_components=r.get("affected_components"),
            embedding=embeddings.embed(text),
        )
    return len(rows)


def _truncate(conn) -> None:
    """Clear the seeded tables (not code_nodes/edges — those UPSERT cleanly)."""
    with conn.cursor() as cur:
        # resolution_steps cascades from incidents
        cur.execute("DELETE FROM incidents")
        cur.execute("DELETE FROM doc_chunks")
        cur.execute("DELETE FROM code_changes")


def seed_all(apply_schema: bool = False, truncate: bool = False) -> dict[str, int]:
    """Full seed run. Returns per-source counts."""
    conn = db.get_conn()
    try:
        if apply_schema:
            db.apply_schema(conn, str(REPO_ROOT / "sql" / "schema.sql"))
        if truncate:
            _truncate(conn)
        n_nodes, n_edges = load_code_graph(conn)
        counts = {
            "code_nodes": n_nodes,
            "code_edges": n_edges,
            "incidents": load_incidents(conn),
            "doc_chunks": load_docs(conn),
            "code_changes": load_code_changes(conn),
        }
        return counts
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed CockroachDB with felix's memory corpora.")
    ap.add_argument("--apply-schema", action="store_true", help="run sql/schema.sql first (idempotent)")
    ap.add_argument("--truncate", action="store_true", help="clear incidents/docs/code_changes before seeding")
    args = ap.parse_args()

    counts = seed_all(apply_schema=args.apply_schema, truncate=args.truncate)
    print("seeded:")
    for k, v in counts.items():
        print(f"  {k:14} {v}")


if __name__ == "__main__":
    main()
