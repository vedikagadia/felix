"""Shared base for repositories."""

from __future__ import annotations

import psycopg


class BaseRepository:
    """Holds the psycopg connection every repository operates against.

    Repositories are cheap, stateless-apart-from-the-connection handles; create
    them per connection and let the caller own the connection's lifetime.
    """

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
