"""Settings — the single place that reads configuration from the environment.

Values live in a .env file (see .env.example); this loads them once into a
typed, frozen Settings object. Swap environments by swapping the .env file
(local CockroachDB + local embedder vs. Cloud cluster + Bedrock) — no code
changes. This is felix's analog of a config bean: one loader, many env files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    # persistence
    database_url: str

    # embedder (clients/embedder): "local" (bge-large) | "titan" (Bedrock)
    embed_provider: str

    # AWS / Bedrock (used only when embed_provider == "titan", and later the LLM)
    aws_region: str
    bedrock_embed_model_id: str

    # CockroachDB Managed MCP Server (recall-path spike; optional)
    crdb_mcp_url: str | None
    crdb_mcp_api_key: str | None

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            embed_provider=os.environ.get("EMBED_PROVIDER", "local").strip().lower(),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            bedrock_embed_model_id=os.environ.get(
                "BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"
            ),
            crdb_mcp_url=os.environ.get("CRDB_MCP_URL") or None,
            crdb_mcp_api_key=os.environ.get("CRDB_MCP_API_KEY") or None,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Call Settings.load() directly if you need a fresh read."""
    return Settings.load()
