"""Swappable text embedder behind one interface.

Provider is selected by the `EMBED_PROVIDER` env var:
    titan  — AWS Bedrock Titan Text Embeddings V2 (amazon.titan-embed-text-v2:0)
    local  — sentence-transformers, BAAI/bge-large-en-v1.5 (default)

Both providers return exactly 1024 floats — the dimension the schema's VECTOR(1024)
columns are built around (see sql/schema.sql). Callers should not care which provider
is active; only `embed` / `embed_batch` are the public surface.

Nothing here touches boto3, AWS credentials, or the local model at *import* time —
those are only instantiated the first time embed()/embed_batch() is actually called,
so `import incidentmemory.embeddings` is always safe.
"""

from __future__ import annotations

import os

EMBED_DIM = 1024

# ── module-level singletons, lazily created ─────────────────────────────────
_bedrock_client = None       # boto3 bedrock-runtime client, for the "titan" provider
_local_model = None          # sentence_transformers.SentenceTransformer, for "local"


def _get_provider() -> str:
    return os.environ.get("EMBED_PROVIDER", "local").strip().lower()


def _get_bedrock_client():
    """Lazily create (once) the boto3 bedrock-runtime client used by the titan provider."""
    global _bedrock_client
    if _bedrock_client is None:
        import boto3  # imported here so boto3 is only required when titan is actually used

        region = os.environ.get("AWS_REGION", "us-east-1")
        _bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_client


def _get_local_model():
    """Lazily load (once) the local sentence-transformers model."""
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer  # heavy import, deferred

        _local_model = SentenceTransformer("BAAI/bge-large-en-v1.5")
    return _local_model


def _embed_titan(text: str) -> list[float]:
    """Call Bedrock Titan Text Embeddings V2 for a single text, requesting 1024 dims.

    Titan V2 request shape:
        {"inputText": "...", "dimensions": 1024, "normalize": true}
    Response shape:
        {"embedding": [...], "inputTextTokenCount": N}
    """
    import json

    client = _get_bedrock_client()
    model_id = os.environ.get("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
    body = json.dumps(
        {
            "inputText": text,
            "dimensions": EMBED_DIM,
            "normalize": True,
        }
    )
    response = client.invoke_model(
        modelId=model_id,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    vec = payload["embedding"]
    return [float(x) for x in vec]


def _embed_local(text: str) -> list[float]:
    """Embed a single text with the local sentence-transformers model."""
    model = _get_local_model()
    vec = model.encode(text, normalize_embeddings=True)
    return [float(x) for x in vec]


def embed(text: str) -> list[float]:
    """Embed a single string, returning a 1024-dim vector.

    Raises ValueError if EMBED_PROVIDER is set to something other than 'titan' or 'local'.
    """
    provider = _get_provider()
    if provider == "titan":
        vec = _embed_titan(text)
    elif provider == "local":
        vec = _embed_local(text)
    else:
        raise ValueError(
            f"Unknown EMBED_PROVIDER={provider!r}; expected 'titan' or 'local'"
        )

    assert len(vec) == EMBED_DIM, (
        f"embed() produced a {len(vec)}-dim vector via provider={provider!r}, "
        f"expected {EMBED_DIM}"
    )
    return vec


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings, returning a list of 1024-dim vectors, one per input.

    This is a straightforward per-item loop over embed() — Titan V2's invoke_model is
    single-text-per-call, and the local model's .encode() batching isn't required at
    hackathon seed-data scale. Revisit if throughput becomes a bottleneck.
    """
    return [embed(t) for t in texts]
