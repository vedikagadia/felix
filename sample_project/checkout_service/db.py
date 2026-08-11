"""Database connection pool wrapper.

A thin, fake pool — no real database. The important behaviour for incident
diagnosis is that connections are a scarce resource (DB_POOL_SIZE) and that a
caller which holds a connection while doing slow work can starve everyone else.
"""

import logging

from checkout_service import config

log = logging.getLogger("checkout.db")


class ConnectionPoolExhausted(Exception):
    """Raised when no DB connection is available within the timeout."""


class ConnectionPool:
    def __init__(self, size=config.DB_POOL_SIZE):
        self.size = size
        self.in_use = 0

    def acquire(self):
        if self.in_use >= self.size:
            log.error("db.pool.exhausted in_use=%s size=%s", self.in_use, self.size)
            raise ConnectionPoolExhausted(f"no connection available (size={self.size})")
        self.in_use += 1
        log.debug("db.pool.acquire in_use=%s size=%s", self.in_use, self.size)
        return _Connection(self)

    def release(self):
        self.in_use = max(0, self.in_use - 1)
        log.debug("db.pool.release in_use=%s", self.in_use)


class _Connection:
    def __init__(self, pool):
        self._pool = pool

    def execute(self, query):
        log.debug("db.execute query=%s", query)
        return []

    def close(self):
        self._pool.release()


_POOL = ConnectionPool()


def get_pool():
    return _POOL
