"""TitanEmbedder — AWS Bedrock Titan Text Embeddings V2."""

from __future__ import annotations

import json

from ...config import get_settings
from . import EMBED_DIM, Embedder


class TitanEmbedder(Embedder):
    def __init__(self):
        self._client = None  # lazily created boto3 bedrock-runtime client
        settings = get_settings()
        self._region = settings.aws_region
        self._model_id = settings.bedrock_embed_model_id

    def _get_client(self):
        if self._client is None:
            import boto3  # imported here so boto3 is only required when titan is used

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def embed(self, text: str) -> list[float]:
        """Call Titan V2 for a single text, requesting 1024 dims.

        Request:  {"inputText": "...", "dimensions": 1024, "normalize": true}
        Response: {"embedding": [...], "inputTextTokenCount": N}
        """
        client = self._get_client()
        body = json.dumps({"inputText": text, "dimensions": EMBED_DIM, "normalize": True})
        response = client.invoke_model(
            modelId=self._model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        vec = [float(x) for x in payload["embedding"]]
        assert len(vec) == EMBED_DIM, f"Titan returned {len(vec)} dims, expected {EMBED_DIM}"
        return vec
