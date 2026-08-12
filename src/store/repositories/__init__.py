"""Repositories — one per memory source, each mapping DB rows <-> domain models.

Each repository wraps a psycopg connection and owns the SQL for its table(s).
Callers work in domain models (src/models.py); positional row tuples never
escape this package.
"""

from .actions import ActionRepository
from .changes import ChangeRepository
from .docs import DocRepository
from .graph import GraphRepository
from .incidents import IncidentRepository

__all__ = [
    "ActionRepository",
    "ChangeRepository",
    "DocRepository",
    "GraphRepository",
    "IncidentRepository",
]
