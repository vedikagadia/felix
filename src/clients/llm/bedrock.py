"""BedrockClient — AWS Bedrock Claude (Anthropic messages API via invoke_model).

Written but NOT the default (LLM_PROVIDER defaults to "gemini"; see __init__.py).
"""

from __future__ import annotations

import json

from ...config import get_settings
from ...models import LLMResult
from . import LLMClient

MAX_TOKENS = 1024
ANTHROPIC_VERSION = "bedrock-2023-05-31"


class BedrockClient(LLMClient):
    def __init__(self):
        self._client = None  # lazily created boto3 bedrock-runtime client
        settings = get_settings()
        self._region = settings.aws_region
        # Dedicated Claude-on-Bedrock model id (distinct from the Titan
        # embeddings model). Set via BEDROCK_MODEL_ID; defaults to a Claude 3.5
        # Haiku id in Settings.
        self._model_id = settings.bedrock_model_id

    def _get_client(self):
        if self._client is None:
            import boto3  # imported here so boto3 is only required when bedrock is used

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResult:
        """Call Claude on Bedrock for a single prompt via the Anthropic messages body.

        Request:  {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1024,
                    "system": "...", "messages": [{"role": "user", "content": "..."}]}
        Response: {"content": [{"text": "..."}], "usage": {"input_tokens": N,
                    "output_tokens": N}}
        """
        client = self._get_client()
        body: dict = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        response = client.invoke_model(
            modelId=self._model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())

        content = payload.get("content") or []
        text = content[0].get("text", "") if content else ""

        usage = payload.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")

        return LLMResult(
            text=text,
            model=self._model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
