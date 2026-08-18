"""Ingest a repo's `git log` into `code_changes` — the "what changed?" signal.

Two `git log` passes (metadata + per-commit file lists, joined on sha) turn each
non-merge commit into a `code_changes` row embedded on `title + summary`, so a
future alert can recall "a recent merge touched X". felix's change recall is
time-windowed (last 14 days), so ingesting full history is fine — only recent
commits surface on the live path, older ones stay browsable.

Stdlib only (`subprocess`). Degrades gracefully: a non-git directory (or missing
`git`) ingests nothing rather than raising, so onboarding a plain folder is OK.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import ingest_id

if TYPE_CHECKING:
    from ...clients.embedder import Embedder
    from ...store.repositories import ChangeRepository

# ASCII control chars as field/record separators — safe because commit text
# never contains them (git's %x1f = US, %x1e = RS).
_US = "\x1f"
_RS = "\x1e"


def _git(root: Path, *args: str) -> str | None:
    """Run a git command in `root`, returning stdout, or None if git isn't
    available / this isn't a repo / the command failed."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None  # git not installed
    if proc.returncode != 0:
        return None
    return proc.stdout


def _is_git_repo(root: Path) -> bool:
    out = _git(root, "rev-parse", "--is-inside-work-tree")
    return out is not None and out.strip() == "true"


def _files_by_sha(root: Path, max_commits: int) -> dict[str, list[str]]:
    """sha -> changed file paths, via one `--name-only` pass."""
    out = _git(
        root,
        "log",
        f"-n{max_commits}",
        "--no-merges",
        "--name-only",
        f"--pretty=format:{_RS}%H{_US}",
    )
    files: dict[str, list[str]] = {}
    if not out:
        return files
    for record in out.split(_RS):
        record = record.strip("\n")
        if not record or _US not in record:
            continue
        sha, _, rest = record.partition(_US)
        paths = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        files[sha.strip()] = paths
    return files


def ingest_git_changes(
    repo: "ChangeRepository",
    embedder: "Embedder",
    root: str | Path,
    *,
    project: str,
    service: str,
    max_commits: int = 200,
) -> int:
    """Ingest up to `max_commits` recent non-merge commits into `code_changes`.

    `repo` must already be scoped to `project`. Returns the number of commits
    ingested (0 if `root` isn't a git repo)."""
    root = Path(root).resolve()
    if not _is_git_repo(root):
        return 0

    meta = _git(
        root,
        "log",
        f"-n{max_commits}",
        "--no-merges",
        "--date=iso-strict",
        f"--pretty=format:%H{_US}%an{_US}%aI{_US}%s{_US}%b{_RS}",
    )
    if not meta:
        return 0

    files_by_sha = _files_by_sha(root, max_commits)

    count = 0
    for record in meta.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_US)
        if len(parts) < 5:
            continue
        sha, author, merged_at, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4]
        sha = sha.strip()
        if not sha:
            continue
        summary = body.strip() or None
        title = subject.strip() or sha[:12]
        text = f"{title}\n{summary or ''}"
        repo.insert(
            id=ingest_id(project, "commit", sha),
            commit_sha=sha,
            merged_at=merged_at.strip(),
            author=author.strip() or None,
            title=title,
            summary=summary,
            files_changed=files_by_sha.get(sha, []),
            services_affected=[service],
            affected_components=[],  # not resolved to code-node names yet
            embedding=embedder.embed(text),
        )
        count += 1
    return count
