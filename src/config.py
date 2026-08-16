"""Settings — the single place that reads configuration from the environment.

Values live in a .env file (see .env.example); this loads them once into a
typed, frozen Settings object. Swap environments by swapping the .env file
(local CockroachDB + local embedder vs. Cloud cluster + Bedrock) — no code
changes. This is felix's analog of a config bean: one loader, many env files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


def _parse_thresholds(raw: str | None) -> dict[str, float]:
    """Parse METRIC_ALERT_THRESHOLDS (a JSON object of metric -> p99 ms) into a
    dict, tolerating an unset/blank/malformed value by returning {}. Lets ops
    pin per-metric alert levels from the environment without a code change."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): float(v) for k, v in parsed.items()}
    except (ValueError, TypeError, AttributeError):
        return {}


@dataclass(frozen=True)
class Settings:
    # persistence
    database_url: str

    # embedder (clients/embedder): "local" (bge-large) | "titan" (Bedrock)
    embed_provider: str

    # AWS / Bedrock
    aws_region: str
    bedrock_embed_model_id: str  # Titan embeddings model (embed_provider == "titan")
    bedrock_model_id: str  # Claude text model (llm_provider == "bedrock")

    # CockroachDB Cloud Managed MCP Server (optional). Auth is OAuth (browser
    # flow); `crdb_mcp_cluster_id` is sent as the required `mcp-cluster-id`
    # header. `crdb_mcp_api_key` is only used if a service-account bearer token
    # is available for headless access (otherwise the OAuth flow is used).
    crdb_mcp_url: str | None
    crdb_mcp_cluster_id: str | None
    crdb_mcp_api_key: str | None

    # LLM reasoning (clients/llm): "gemini" | "bedrock"
    llm_provider: str
    gemini_api_key: str | None
    gemini_model_id: str

    # Live-monitoring alert levels. `metric_alert_default_p99_ms` is the p99
    # threshold a latency metric trips at when it has no specific entry;
    # `metric_alert_thresholds` maps a metric name to its own p99 threshold
    # (e.g. {"payment_latency_ms": 800}). Both are the *defaults* the panel
    # loads — an operator can still override any card's level live in the UI.
    metric_alert_default_p99_ms: float
    metric_alert_thresholds: dict[str, float]

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
            bedrock_model_id=os.environ.get(
                "BEDROCK_MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0"
            ),
            crdb_mcp_url=os.environ.get("CRDB_MCP_URL") or None,
            crdb_mcp_cluster_id=os.environ.get("CRDB_MCP_CLUSTER_ID") or None,
            crdb_mcp_api_key=os.environ.get("CRDB_MCP_API_KEY") or None,
            llm_provider=os.environ.get("LLM_PROVIDER", "gemini").strip().lower(),
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            gemini_model_id=os.environ.get("GEMINI_MODEL_ID", "gemini-flash-latest"),
            metric_alert_default_p99_ms=float(
                os.environ.get("METRIC_ALERT_DEFAULT_P99_MS", "1000")
            ),
            metric_alert_thresholds=_parse_thresholds(
                os.environ.get("METRIC_ALERT_THRESHOLDS")
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Call Settings.load() directly if you need a fresh read."""
    return Settings.load()
