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
from ..store.repositories import (
    ChangeRepository,
    DocRepository,
    GraphRepository,
    IncidentRepository,
    RunbookRepository,
    TopologyRepository,
)
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


def _load_json(path: Path) -> Any:
    """Parse a seed JSON file. Most corpora are top-level lists
    (incidents/docs/changes); topology.json is a top-level object
    ({"nodes": [...], "edges": [...]}). json.load returns whichever shape the
    file holds — the caller knows which it expects."""
    with path.open() as f:
        return json.load(f)


class Seeder:
    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder | None = None,
        project: str = "sample",
    ):
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.project = project
        self.incidents = IncidentRepository(conn, project)
        self.docs = DocRepository(conn, project)
        self.changes = ChangeRepository(conn, project)
        self.graph = GraphRepository(conn, project)
        self.topology = TopologyRepository(conn, project)
        self.runbooks = RunbookRepository(conn, project)

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

    def load_topology(self, path: Path | None = None) -> tuple[int, int]:
        """Load the service-topology graph (service_nodes/service_edges) and
        UPSERT it — mirrors load_code_graph. Node ids are deterministic uuid5 of
        the seed's stable string id (e.g. 'svc-checkout-service'), so the load is
        idempotent with no truncate needed. Edges resolve their src/dst service
        names to node ids via a name->stable-id map built while loading nodes.
        No embeddings (the topology is traversed, never vector-searched)."""
        data = _load_json(path or SEED_DIR / "topology.json")
        name_to_id: dict[str, str] = {}
        for n in data.get("nodes", []):
            node_id = _seed_uuid(n["id"])
            name_to_id[n["name"]] = node_id
            self.topology.upsert_node(
                id=node_id,
                name=n["name"],
                kind=n.get("kind", "service"),
                summary=n.get("summary"),
                health_checks=n.get("health_checks"),
            )
        edges = data.get("edges", [])
        for e in edges:
            self.topology.upsert_edge(
                src_id=name_to_id[e["src"]],
                dst_id=name_to_id[e["dst"]],
                kind=e.get("kind", "depends_on"),
            )
        return len(data.get("nodes", [])), len(edges)

    def load_runbooks(self, path: Path | None = None) -> int:
        rows = _load_json(path or SEED_DIR / "runbooks.json")
        for r in rows:
            text = f"{r['title']}\n{r['symptoms']}"
            self.runbooks.insert(
                id=_seed_uuid(r["id"]),
                title=r["title"],
                symptoms=r["symptoms"],
                service=r.get("service"),
                tags=r.get("tags"),
                embedding=self.embedder.embed(text),
                steps=r.get("steps"),
            )
        return len(rows)

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
        """Clear THIS project's seeded tables (not code_nodes/edges — those UPSERT
        cleanly). Scoped to `self.project` so re-seeding the demo never wipes an
        onboarded project's memory."""
        with self.conn.cursor() as cur:
            # resolution_steps cascades from incidents; runbook_steps from runbooks
            cur.execute("DELETE FROM incidents WHERE project = %s", (self.project,))
            cur.execute("DELETE FROM doc_chunks WHERE project = %s", (self.project,))
            cur.execute("DELETE FROM code_changes WHERE project = %s", (self.project,))
            cur.execute("DELETE FROM runbooks WHERE project = %s", (self.project,))

    def seed_all(self, truncate: bool = False) -> dict[str, int]:
        """Full seed run against this Seeder's connection. Returns per-source counts."""
        if truncate:
            self.truncate()
        n_nodes, n_edges = self.load_code_graph()
        n_svc_nodes, n_svc_edges = self.load_topology()
        return {
            "code_nodes": n_nodes,
            "code_edges": n_edges,
            "service_nodes": n_svc_nodes,
            "service_edges": n_svc_edges,
            "incidents": self.load_incidents(),
            "doc_chunks": self.load_docs(),
            "code_changes": self.load_code_changes(),
            "runbooks": self.load_runbooks(),
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
