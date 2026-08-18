"""Ingest a repo's `runbooks/` directory into `runbooks`.

Each markdown file under any `runbooks/` directory becomes one runbook: its H1
(or filename) is the title, the trigger text (`symptoms`) is a "Symptoms"/"When"
section if present else the first prose paragraph, and ordered/bulleted list
items become `runbook_steps` (inline `code` promoted to a step command). The
parent row is embedded on `title + symptoms` so felix recalls the right playbook
by meaning, mirroring the authored-runbook seeder.

Stdlib only (`re`, `pathlib`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import ingest_id, iter_source_files

if TYPE_CHECKING:
    from ...clients.embedder import Embedder
    from ...store.repositories import RunbookRepository

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+(.*)")
_TRIGGER_HEADINGS = ("symptom", "when", "trigger", "alert", "detect")


def _sections(text: str) -> list[tuple[str | None, str]]:
    """(heading, body) sections split at ATX headings (same shape as the docs
    chunker, but kept local so the two ingesters stay independent)."""
    out: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body or heading:
            out.append((heading, body))

    for line in text.splitlines():
        m = _ATX_HEADING.match(line)
        if m:
            flush()
            heading = m.group(2).strip() or None
            buf = []
        else:
            buf.append(line)
    flush()
    return out


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        m = _ATX_HEADING.match(line)
        if m:
            return m.group(2).strip() or fallback
    return fallback


def _first_paragraph(text: str) -> str:
    """First non-heading, non-list prose paragraph — the fallback trigger text."""
    para: list[str] = []
    for line in text.splitlines():
        if _ATX_HEADING.match(line):
            if para:
                break
            continue
        if _LIST_ITEM.match(line):
            if para:
                break
            continue
        if line.strip():
            para.append(line.strip())
        elif para:
            break
    return " ".join(para).strip()


def _symptoms(sections: list[tuple[str | None, str]], full_text: str, title: str) -> str:
    """Trigger text: a Symptoms/When/Trigger section if the runbook has one,
    else the first prose paragraph, else the title."""
    for heading, body in sections:
        if heading and any(k in heading.lower() for k in _TRIGGER_HEADINGS) and body:
            return body.strip()
    para = _first_paragraph(full_text)
    return para or title


def _steps(sections: list[tuple[str | None, str]], full_text: str) -> list[dict]:
    """Ordered/bulleted list items -> runbook_steps. Prefers a Steps/Resolution/
    Remediation section; otherwise takes every list item in the doc."""
    preferred = [
        body
        for heading, body in sections
        if heading and any(k in heading.lower() for k in ("step", "resolution", "remediat", "fix", "runbook", "action"))
    ]
    scan = "\n".join(preferred) if preferred else full_text

    steps: list[dict] = []
    order = 0
    for line in scan.splitlines():
        m = _LIST_ITEM.match(line)
        if not m:
            continue
        action = m.group(1).strip()
        if not action:
            continue
        order += 1
        code = re.search(r"`([^`]+)`", action)
        steps.append(
            {
                "step_order": order,
                "action": action,
                "command": code.group(1) if code else None,
                "outcome": None,
            }
        )
    return steps


def ingest_runbooks(
    repo: "RunbookRepository",
    embedder: "Embedder",
    root: str | Path,
    *,
    project: str,
    service: str | None = None,
) -> int:
    """Ingest markdown files under any `runbooks/` directory below `root` into
    `runbooks` (project-scoped `repo`). Returns the number of runbooks inserted
    (0 if the repo has no runbooks directory)."""
    root = Path(root).resolve()
    count = 0
    for path in iter_source_files(root, (".md", ".markdown")):
        rel = path.relative_to(root)
        if not any(part.lower() == "runbooks" for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_str = str(rel)
        sections = _sections(text)
        title = _title(text, path.stem)
        symptoms = _symptoms(sections, text, title)
        steps = _steps(sections, text)
        repo.insert(
            id=ingest_id(project, "runbook", rel_str),
            title=title,
            symptoms=symptoms,
            service=service,
            tags=None,
            embedding=embedder.embed(f"{title}\n{symptoms}"),
            steps=steps or None,
        )
        count += 1
    return count
