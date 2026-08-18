"""Shared base for repositories."""

from __future__ import annotations

import psycopg

# The built-in demo project. Every memory row carries a `project` slug (see
# sql/schema.sql `projects`); when a caller doesn't specify one we scope to the
# demo, so the CLI, tests, and any pre-multi-project caller keep working exactly
# as before. felix can onboard other projects under different slugs.
DEFAULT_PROJECT = "sample"


class BaseRepository:
    """Holds the psycopg connection every repository operates against, plus the
    `project` (tenant) slug that scopes every read and write.

    Repositories are cheap, stateless-apart-from-the-connection handles; create
    them per connection and let the caller own the connection's lifetime. Pass
    `project=` to scope this repo to one onboarded project's memory; it defaults
    to the built-in demo (`DEFAULT_PROJECT`).
    """

    def __init__(self, conn: psycopg.Connection, project: str = DEFAULT_PROJECT):
        self.conn = conn
        self.project = project
