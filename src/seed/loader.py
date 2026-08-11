"""Seeder — populate CockroachDB with felix's four memory sources.

Ties the pieces together end to end:
  1. parse the sample project into a code graph (parser.parse_project)
  2. load the three seed corpora (incidents / docs / code_changes JSON)
  3. embed the searchable text of each row (Embedder — Titan or local)
  4. insert everything via the repositories (which target sql/schema.sql)

Idempotent enough to re-run: code_nodes/code_edges UPSERT by deterministic id;
incidents/docs/code_changes use the seed's stable string ids as UUIDs so a
re-run would conflict — pass truncate=True to clear those three tables first.

Embedding text conventions match the schema comments:
  incident   -> title + symptoms
  doc chunk  -> heading + body
  code change-> title + summary
Code nodes/edges are graph-traversed, not vector-searched, so carry no embedding.

    python -m src.cli seed --apply-schema
    python -m src.cli seed --truncate         # reseed from scratch
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg

from ..clients.embedder import Embedder, get_embedder
from ..store.connection import apply_schema, get_conn
from ..store.repositories import ChangeRepository, DocRepository, GraphRepository, IncidentRepository
from .parser import parse_project

# repo-root-relative defaults (this file is src/seed/loader.py)
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


class Seeder:
    def __init__(self, conn: psycopg.Connection, embedder: Embedder | None = None):
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.incidents = IncidentRepository(conn)
        self.docs = DocRepository(conn)
        self.changes = ChangeRepository(conn)
        self.graph = GraphRepository(conn)

    # ── individual loaders ────────────────────────────────────────────────────

    def load_code_graph(self) -> tuple[int, int]:
        """Parse the sample project and UPSERT its nodes + edges. No embeddings."""
        nodes, edges = parse_project(str(SAMPLE_ROOT))
        for n in nodes:
            self.graph.upsert_node(
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
            self.graph.upsert_edge(src_id=e["src_id"], dst_id=e["dst_id"], kind=e["kind"])
        return len(nodes), len(edges)

    def load_incidents(self, path: Path | None = None) -> int:
        rows = _load_json(path or SEED_DIR / "incidents.json")
        for r in rows:
            text = f"{r['title']}\n{r['symptoms']}"
            self.incidents.insert(
                id=_seed_uuid(r["id"]),
                title=r["title"],
                symptoms=r["symptoms"],
                root_cause=r.get("root_cause"),
                service=r.get("service"),
                severity=r.get("severity"),
                tags=r.get("tags"),
                occurred_at=r.get("occurred_at"),
                embedding=self.embedder.embed(text),
                resolution_steps=r.get("resolution_steps"),
            )
        return len(rows)

    def load_docs(self, path: Path | None = None) -> int:
        rows = _load_json(path or SEED_DIR / "docs.json")
        for r in rows:
            text = f"{r.get('heading', '')}\n{r['body']}"
            self.docs.insert(
                id=_seed_uuid(r["id"]),
                doc_title=r["doc_title"],
                heading=r.get("heading"),
                body=r["body"],
                doc_type=r.get("doc_type"),
                source_path=r.get("source_path"),
                embedding=self.embedder.embed(text),
            )
        return len(rows)

    def load_code_changes(self, path: Path | None = None) -> int:
        rows = _load_json(path or SEED_DIR / "code_changes.json")
        for r in rows:
            text = f"{r['title']}\n{r.get('summary', '')}"
            self.changes.insert(
                id=_seed_uuid(r["id"]),
                commit_sha=r["commit_sha"],
                merged_at=r["merged_at"],
                author=r.get("author"),
                title=r["title"],
                summary=r.get("summary"),
                files_changed=r.get("files_changed"),
                services_affected=r.get("services_affected"),
                affected_components=r.get("affected_components"),
                embedding=self.embedder.embed(text),
            )
        return len(rows)

    def truncate(self) -> None:
        """Clear the seeded tables (not code_nodes/edges — those UPSERT cleanly)."""
        with self.conn.cursor() as cur:
            # resolution_steps cascades from incidents
            cur.execute("DELETE FROM incidents")
            cur.execute("DELETE FROM doc_chunks")
            cur.execute("DELETE FROM code_changes")

    def seed_all(self, truncate: bool = False) -> dict[str, int]:
        """Full seed run against this Seeder's connection. Returns per-source counts."""
        if truncate:
            self.truncate()
        n_nodes, n_edges = self.load_code_graph()
        return {
            "code_nodes": n_nodes,
            "code_edges": n_edges,
            "incidents": self.load_incidents(),
            "doc_chunks": self.load_docs(),
            "code_changes": self.load_code_changes(),
        }


def run(apply_schema_first: bool = False, truncate: bool = False) -> dict[str, int]:
    """Open a connection, optionally apply schema, seed, and return counts."""
    conn = get_conn()
    try:
        if apply_schema_first:
            apply_schema(conn, str(REPO_ROOT / "sql" / "schema.sql"))
        return Seeder(conn).seed_all(truncate=truncate)
    finally:
        conn.close()
