"""Swappable text embedder behind one interface.

Provider is selected by EMBED_PROVIDER (via Settings):
    titan  — AWS Bedrock Titan Text Embeddings V2 (amazon.titan-embed-text-v2:0)
    local  — sentence-transformers, BAAI/bge-large-en-v1.5 (default)

Both return exactly 1024 floats — the dimension the schema's VECTOR(1024)
columns are built around. Callers depend only on the Embedder ABC; get_embedder()
picks the implementation from config. Nothing here touches boto3, AWS creds, or
the local model at *import* time — those are constructed on first use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from ...config import get_settings

EMBED_DIM = 1024


class Embedder(ABC):
    """One text -> one 1024-dim vector; batch is a per-item loop by default."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of strings, one vector per input.

        A straightforward per-item loop — Titan V2's invoke_model is
        single-text-per-call, and local .encode() batching isn't required at
        hackathon seed scale. Override if throughput becomes a bottleneck.
        """
        return [self.embed(t) for t in texts]


def get_embedder(provider: str | None = None) -> Embedder:
    """Construct the configured Embedder. `provider` overrides Settings if given.

    Process-wide singleton, keyed by the resolved provider: the local bge-large
    model is ~1.5GB in memory, so every caller (each API request, the seed
    loader, the in-process CDC watcher) must share ONE instance. This is what
    lets `serve` run the watcher in a background thread without doubling RAM —
    see the merged-task deploy in DEPLOY.md §4.

    The cache lives on `_build_embedder`, keyed on the *resolved* provider
    string — so `get_embedder()`, `get_embedder(None)`, and
    `get_embedder("local")` all collapse to one instance. (Caching on this
    function instead would key on the raw argument and load the model twice.)

    Raises ValueError for anything other than 'titan' or 'local'.
    """
    provider = (provider or get_settings().embed_provider).strip().lower()
    return _build_embedder(provider)


@lru_cache(maxsize=None)
def _build_embedder(provider: str) -> Embedder:
    if provider == "titan":
        from .titan import TitanEmbedder

        return TitanEmbedder()
    if provider == "local":
        from .local import LocalEmbedder

        return LocalEmbedder()
    raise ValueError(f"Unknown EMBED_PROVIDER={provider!r}; expected 'titan' or 'local'")


__all__ = ["Embedder", "EMBED_DIM", "get_embedder"]
