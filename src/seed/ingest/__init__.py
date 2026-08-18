"""Ingesters — turn an ARBITRARY project's artifacts into felix's memory sources.

Where `loader.Seeder` loads the authored demo corpora (fiction, from JSON), these
ingesters extract memory from a REAL repo the operator onboards:

  git log      -> code_changes   (git_changes.ingest_git_changes)
  *.md / *.rst -> doc_chunks      (docs.ingest_docs)
  runbooks/    -> runbooks        (runbooks.ingest_runbooks)

The code graph (code_nodes/code_edges) comes from `parser.parse_python_project`,
and incidents start empty — a project accumulates episodic incidents only through
the learning loop as felix diagnoses real alerts. Each ingester embeds its rows'
searchable text (via the shared Embedder) and inserts through a project-scoped
repository, so everything lands in the onboarded project's namespace.

Ids are deterministic (`ingest_id`) so a re-ingest of the same project addresses
the same rows; the onboarding service truncates the project first, so plain
INSERTs never collide across re-runs.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterator

# Reuse the parser's vendor/build/VCS skip set so docs & runbooks discovery
# ignores the same noise the code walk does.
from ..parser import SKIP_DIRS

# Namespace for ingested-row ids (distinct from the seed corpora's SEED_NS).
INGEST_NS = uuid.UUID("f00dfeed-1111-1111-1111-111111111111")


def ingest_id(project: str, *parts: str) -> str:
    """Deterministic uuid5 for an ingested row, scoped to `project` so two
    projects with the same natural key (commit sha, doc path) never collide."""
    return str(uuid.uuid5(INGEST_NS, ":".join([project, *parts])))


def iter_source_files(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Yield files under `root` whose suffix is in `suffixes` (case-insensitive),
    skipping any path that descends through a `SKIP_DIRS` directory. Sorted for
    deterministic ordering (so chunk indices are stable across runs)."""
    lowered = tuple(s.lower() for s in suffixes)
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in lowered:
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file():
            yield path


from .docs import ingest_docs  # noqa: E402
from .git_changes import ingest_git_changes  # noqa: E402
from .runbooks import ingest_runbooks  # noqa: E402

__all__ = [
    "INGEST_NS",
    "ingest_id",
    "iter_source_files",
    "ingest_docs",
    "ingest_git_changes",
    "ingest_runbooks",
]
