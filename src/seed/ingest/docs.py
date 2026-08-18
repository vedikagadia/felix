"""Ingest a repo's Markdown/reStructuredText into `doc_chunks`.

Each `*.md` / `*.rst` file is split into heading-anchored chunks (an ATX `#`
heading and the prose beneath it, up to the next heading), oversized sections
further split on blank lines so no single embedding swallows a whole file. Each
chunk becomes a `doc_chunks` row embedded on `heading + body`, so felix can
recall "the doc that explains X". Runbooks live in their own source, so a
`runbooks/` subtree is skipped here to avoid double-counting.

Stdlib only (`re`, `pathlib`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import ingest_id, iter_source_files

if TYPE_CHECKING:
    from ...clients.embedder import Embedder
    from ...store.repositories import DocRepository

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_MAX_CHUNK_CHARS = 2000


def _split_oversized(body: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Split a too-long section on blank lines (paragraph boundaries), greedily
    packing paragraphs up to `limit` chars. Keeps whole paragraphs together."""
    if len(body) <= limit:
        return [body]
    paras = re.split(r"\n\s*\n", body)
    out: list[str] = []
    cur = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out or [body]


def _chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """Split markdown into (heading, body) chunks at ATX headings. Prose before
    the first heading becomes a headingless preamble chunk."""
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body or heading:
            chunks.append((heading, body))

    for line in text.splitlines():
        m = _ATX_HEADING.match(line)
        if m:
            flush()
            heading = m.group(2).strip() or None
            buf = []
        else:
            buf.append(line)
    flush()
    return [(h, b) for h, b in chunks if b]


def _doc_title(text: str, fallback: str) -> str:
    """The document title: its first H1, else the file's name."""
    for line in text.splitlines():
        m = _ATX_HEADING.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip() or fallback
    return fallback


def ingest_docs(
    repo: "DocRepository",
    embedder: "Embedder",
    root: str | Path,
    *,
    project: str,
) -> int:
    """Ingest `*.md`/`*.rst` under `root` into `doc_chunks` (project-scoped
    `repo`). Skips a top-level/any `runbooks/` subtree (those are ingested as
    runbooks). Returns the number of chunks inserted."""
    root = Path(root).resolve()
    count = 0
    for path in iter_source_files(root, (".md", ".rst", ".markdown")):
        rel = path.relative_to(root)
        # runbooks are their own memory source — don't also file them as docs
        if any(part.lower() == "runbooks" for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_str = str(rel)
        doc_type = "rst" if path.suffix.lower() == ".rst" else "markdown"
        title = _doc_title(text, path.stem)

        idx = 0
        for heading, body in _chunk_markdown(text):
            for piece in _split_oversized(body):
                embed_text = f"{heading or ''}\n{piece}".strip()
                if not embed_text:
                    continue
                repo.insert(
                    id=ingest_id(project, "doc", rel_str, str(idx)),
                    doc_title=title,
                    heading=heading,
                    body=piece,
                    doc_type=doc_type,
                    source_path=rel_str,
                    embedding=embedder.embed(embed_text),
                )
                idx += 1
                count += 1
    return count
