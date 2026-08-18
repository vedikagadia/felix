"""OnboardingService — turn an arbitrary project into a felix memory namespace.

Given a local path OR a git URL, felix builds a fresh tenant (`project` slug) and
populates its memory from the project's own artifacts:

  code graph   -> code_nodes/code_edges   (parser.parse_python_project)
  git log      -> code_changes            (ingest.ingest_git_changes)
  *.md/*.rst   -> doc_chunks              (ingest.ingest_docs)
  runbooks/    -> runbooks                (ingest.ingest_runbooks)

Incidents start empty — a project accrues episodic incidents only through the
learning loop as felix diagnoses real alerts against it. The project is
registered in `projects` (so the switcher can list it) and stamped `last_synced`
on success. Re-onboarding the same slug resets its ingested sources first (but
PRESERVES learned incidents), so a re-sync is idempotent.

Live monitoring is NOT ingested here: it's push-based (services send their own
`metrics` rows via the Probe / an insert), documented in the Live-monitoring
panel — see the frontend help card.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from ..clients.embedder import Embedder, get_embedder
from ..seed.ingest import ingest_docs, ingest_git_changes, ingest_runbooks
from ..seed.parser import parse_python_project
from ..store.repositories import (
    ChangeRepository,
    DocRepository,
    GraphRepository,
    ProjectRepository,
    RunbookRepository,
)
from ..store.repositories.projects import slugify

log = logging.getLogger(__name__)

# Where git URLs are cloned. Repo-root-relative, gitignored (see .gitignore).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE = REPO_ROOT / ".felix-projects"

# Every ingestable source; the caller may pass a subset.
ALL_SOURCES = ("code", "changes", "docs", "runbooks")


@dataclass
class OnboardResult:
    """Summary of one onboarding run — per-source row counts + registry info."""

    project: str
    display_name: str
    source_kind: str
    source_ref: str
    local_path: str
    counts: dict[str, int] = field(default_factory=dict)


def _looks_like_git_url(source: str) -> bool:
    s = source.strip()
    return (
        s.startswith(("http://", "https://", "git://", "ssh://", "git@"))
        or s.endswith(".git")
    )


class OnboardingService:
    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder | None = None,
        *,
        workspace: str | Path | None = None,
    ):
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.workspace = Path(workspace or DEFAULT_WORKSPACE)
        self.projects = ProjectRepository(conn)

    # ── source resolution ─────────────────────────────────────────────────────

    def _resolve_source(self, source: str, slug: str) -> tuple[Path, str, str]:
        """Return (local_root, source_kind, source_ref). Clones a git URL into
        the workspace; validates a local path exists."""
        if _looks_like_git_url(source):
            dest = self.workspace / slug
            self._clone_or_update(source, dest)
            return dest, "git", source
        path = Path(source).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"local path does not exist or is not a directory: {source}")
        return path, "path", str(path)

    def _clone_or_update(self, url: str, dest: Path) -> None:
        """Clone `url` into `dest`, or best-effort update if it's already there.

        A fresh clone is bounded (`--depth`) so a huge history doesn't stall the
        demo; `git log` ingestion reads at most `max_commits` anyway."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if (dest / ".git").is_dir():
            log.info("onboard: repo present, fetching updates: %s", dest)
            res = subprocess.run(
                ["git", "-C", str(dest), "pull", "--ff-only"],
                capture_output=True, text=True, check=False,
            )
            if res.returncode != 0:
                log.warning("onboard: git pull failed (using existing checkout): %s", res.stderr.strip())
            return
        if dest.exists():
            shutil.rmtree(dest)  # non-git leftover — start clean
        log.info("onboard: cloning %s -> %s", url, dest)
        res = subprocess.run(
            ["git", "clone", "--depth", "300", url, str(dest)],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"git clone failed: {res.stderr.strip() or res.stdout.strip()}")

    # ── project memory reset (idempotent re-onboard) ──────────────────────────

    def _reset_ingested(self, project: str) -> None:
        """Clear a project's INGESTED memory (code graph, changes, docs,
        runbooks) so a re-sync doesn't accumulate stale rows. Deliberately does
        NOT touch `incidents` — episodic memory learned from real diagnoses
        survives a re-onboard. Edges have no project column, so they're deleted
        by membership in this project's code_nodes (before the nodes go)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM code_edges
                WHERE src_id IN (SELECT id FROM code_nodes WHERE project = %s)
                   OR dst_id IN (SELECT id FROM code_nodes WHERE project = %s)
                """,
                (project, project),
            )
            cur.execute("DELETE FROM code_nodes WHERE project = %s", (project,))
            cur.execute("DELETE FROM code_changes WHERE project = %s", (project,))
            cur.execute("DELETE FROM doc_chunks WHERE project = %s", (project,))
            cur.execute("DELETE FROM runbooks WHERE project = %s", (project,))

    # ── the individual ingest steps ───────────────────────────────────────────

    def _load_code_graph(self, root: Path, project: str, service: str) -> int:
        nodes, edges = parse_python_project(str(root), project=project, service=service)
        graph = GraphRepository(self.conn, project)
        for n in nodes:
            graph.upsert_node(
                id=n["id"], name=n["name"], kind=n["kind"], file=n["file"],
                service=n["service"], source=n["source"], summary=n["summary"],
                last_commit=n["last_commit"],
            )
        for e in edges:
            graph.upsert_edge(src_id=e["src_id"], dst_id=e["dst_id"], kind=e["kind"])
        return len(nodes)

    # ── the entry point ────────────────────────────────────────────────────────

    def onboard(
        self,
        source: str,
        *,
        display_name: str | None = None,
        project: str | None = None,
        sources: tuple[str, ...] = ALL_SOURCES,
        max_commits: int = 200,
    ) -> OnboardResult:
        """Onboard `source` (local path or git URL) as a felix project.

        `project` overrides the derived slug; `display_name` the human label;
        `sources` selects which ingesters run. Returns per-source counts. The
        project is registered up front (so it's listable even mid-sync) and
        stamped `last_synced` only after a successful ingest."""
        requested = tuple(s for s in sources if s in ALL_SOURCES) or ALL_SOURCES
        name = display_name or Path(source.rstrip("/").removesuffix(".git")).name or source
        slug = slugify(project or name)
        if slug == "sample":
            raise ValueError("'sample' is the built-in demo project; choose another name")

        root, source_kind, source_ref = self._resolve_source(source, slug)
        service = slug  # arbitrary repos map their whole graph to one service (the slug)

        # register before ingest so a long sync is still visible in the switcher
        self.projects.upsert(
            id=slug, display_name=name, source_kind=source_kind, source_ref=source_ref, synced=False,
        )
        self._reset_ingested(slug)

        counts: dict[str, int] = {}
        if "code" in requested:
            counts["code_nodes"] = self._load_code_graph(root, slug, service)
        if "changes" in requested:
            counts["code_changes"] = ingest_git_changes(
                ChangeRepository(self.conn, slug), self.embedder, root,
                project=slug, service=service, max_commits=max_commits,
            )
        if "docs" in requested:
            counts["doc_chunks"] = ingest_docs(
                DocRepository(self.conn, slug), self.embedder, root, project=slug,
            )
        if "runbooks" in requested:
            counts["runbooks"] = ingest_runbooks(
                RunbookRepository(self.conn, slug), self.embedder, root,
                project=slug, service=service,
            )

        self.projects.upsert(
            id=slug, display_name=name, source_kind=source_kind, source_ref=source_ref, synced=True,
        )
        return OnboardResult(
            project=slug, display_name=name, source_kind=source_kind,
            source_ref=source_ref, local_path=str(root), counts=counts,
        )
