"""ProjectRepository — the tenant registry (`projects` table).

Unlike every other repository, this one is NOT project-scoped: it manages the
list of projects itself. One row per onboarded project (plus the built-in
'sample' demo, inserted by schema.sql). felix's memory tables all carry a
`project` slug that points here by convention (no hard FK, so a project can be
dropped without cascading through the vector tables).
"""

from __future__ import annotations

import re

import psycopg

from ...models import Project


def slugify(name: str) -> str:
    """Turn a display name into a url-safe project slug: lowercase, non-alnum
    runs collapsed to a single hyphen, trimmed. Empty input -> 'project'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


class ProjectRepository:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def list_projects(self) -> list[Project]:
        """Every registered project, the built-in demo first, then newest-first."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, display_name, source_kind, source_ref, created_at, last_synced
                FROM projects
                ORDER BY (id = 'sample') DESC, created_at DESC
                """
            )
            rows = cur.fetchall()
        return [self._row(r) for r in rows]

    def get(self, project_id: str) -> Project | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, display_name, source_kind, source_ref, created_at, last_synced
                FROM projects
                WHERE id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
        return self._row(row) if row else None

    def upsert(
        self,
        *,
        id: str,
        display_name: str,
        source_kind: str,
        source_ref: str | None,
        synced: bool = False,
    ) -> None:
        """Register (or update) a project. `synced=True` stamps last_synced=now()
        — call it after a successful (re-)ingest so the switcher can show freshness."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPSERT INTO projects
                    (id, display_name, source_kind, source_ref, last_synced)
                VALUES
                    (%s, %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)
                """,
                (id, display_name, source_kind, source_ref, synced),
            )

    @staticmethod
    def _row(r) -> Project:
        return Project(
            id=r[0],
            display_name=r[1],
            source_kind=r[2],
            source_ref=r[3],
            created_at=r[4],
            last_synced=r[5],
        )
