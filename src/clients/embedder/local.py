"""LocalEmbedder — sentence-transformers, BAAI/bge-large-en-v1.5 (1024-dim)."""

from __future__ import annotations

from . import EMBED_DIM, Embedder


class LocalEmbedder(Embedder):
    def __init__(self):
        self._model = None  # lazily loaded SentenceTransformer

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import, deferred

            self._model = SentenceTransformer("BAAI/bge-large-en-v1.5")
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._get_model()
        vec = [float(x) for x in model.encode(text, normalize_embeddings=True)]
        assert len(vec) == EMBED_DIM, f"local model returned {len(vec)} dims, expected {EMBED_DIM}"
        return vec
