"""Thin client for the CockroachDB Cloud Managed MCP Server.

Connects to the Managed MCP endpoint and lets callers list/invoke the tools it
exposes (schema introspection, read-only SQL, cluster metadata, …). Nothing here
touches the network at import time — connect()/list_tools()/call_tool() are the
only functions that do — so importing this module is always safe even without
the MCP settings configured or the `mcp` SDK installed.

Auth (important): the CockroachDB Cloud MCP server authenticates via **OAuth**
(a browser consent flow), not a static token. The only per-connection value is
the **cluster id**, which every request must carry as the `mcp-cluster-id`
header. Two ways to authenticate:

  * OAuth (default): the first connect() opens the provider's browser consent
    and caches the resulting tokens under ``.crdb-mcp-tokens.json`` (gitignored)
    so later runs are headless. Requires only CRDB_MCP_URL + CRDB_MCP_CLUSTER_ID.
  * Bearer (optional): if your org issues a service-account token, set
    CRDB_MCP_API_KEY and it's sent as ``Authorization: Bearer <key>`` instead —
    fully headless, no browser.

The simplest way to authenticate for the demo is to add the server to Claude
Code (`.mcp.json`) and run ``claude /mcp`` → Authenticate; that authorizes the
agent's own MCP client. This module is felix's *own* programmatic client for the
same endpoint (used by `mcp-probe` and the DB-overview API path).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ..config import get_settings

# The mcp SDK is imported lazily inside connect() (not at module top) so importing
# this module is safe even when `mcp` isn't installed — matching the lazy-client
# pattern the embedder and LLM clients use for their optional SDKs.

# Where the OAuth tokens are cached between runs (gitignored). Authenticate once
# (browser), reuse headlessly after — refresh is handled by the SDK provider.
_TOKEN_STORE_PATH = Path(__file__).resolve().parents[2] / ".crdb-mcp-tokens.json"

# Loopback redirect the OAuth consent returns to during the interactive flow.
_OAUTH_CALLBACK_PORT = 8765
_OAUTH_REDIRECT_URI = f"http://localhost:{_OAUTH_CALLBACK_PORT}/callback"


def _cluster_headers(settings) -> dict[str, str]:
    """The headers every request to this endpoint must carry. The cluster id is
    required by the CockroachDB Cloud MCP server to scope the connection; a
    bearer token is added only when a service-account key is configured."""
    headers: dict[str, str] = {}
    if settings.crdb_mcp_cluster_id:
        headers["mcp-cluster-id"] = settings.crdb_mcp_cluster_id
    if settings.crdb_mcp_api_key:
        headers["Authorization"] = f"Bearer {settings.crdb_mcp_api_key}"
    return headers


class _FileTokenStorage:
    """A minimal on-disk TokenStorage for the SDK's OAuthClientProvider, so the
    interactive consent only happens once. Implements the four methods the SDK
    calls; tokens/client-info are persisted as JSON next to the repo root."""

    def __init__(self, path: Path = _TOKEN_STORE_PATH) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data))

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


async def _default_redirect_handler(authorization_url: str) -> None:
    """Open the OAuth consent page in the operator's browser."""
    import webbrowser

    print(f"\nAuthorize felix's MCP access in the browser window that opens:\n  {authorization_url}\n")
    webbrowser.open(authorization_url)


async def _default_callback_handler():
    """Run a one-shot localhost server to capture the OAuth redirect (code + state)."""
    import asyncio
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    from mcp.client.auth import AuthorizationCodeResult

    captured: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            params = parse_qs(urlparse(self.path).query)
            captured["code"] = params.get("code", [""])[0]
            captured["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"felix: authentication complete. You can close this tab.")

        def log_message(self, *args):  # silence the default stderr logging
            pass

    def _serve() -> None:
        server = HTTPServer(("localhost", _OAUTH_CALLBACK_PORT), _Handler)
        server.handle_request()  # serve exactly one request, then stop
        server.server_close()

    await asyncio.to_thread(_serve)
    return AuthorizationCodeResult(code=captured.get("code", ""), state=captured.get("state") or None)


def _oauth_provider(settings):
    """Build the SDK OAuthClientProvider for the endpoint (used when no bearer
    token is configured). Tokens are cached via _FileTokenStorage."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientProvider(
        server_url=settings.crdb_mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name="felix",
            redirect_uris=[_OAUTH_REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=_FileTokenStorage(),
        redirect_handler=_default_redirect_handler,
        callback_handler=_default_callback_handler,
    )


@asynccontextmanager
async def connect():
    """Open an MCP ClientSession against the CockroachDB Cloud MCP endpoint.

    Sends the required `mcp-cluster-id` header. Auth is OAuth by default (browser
    consent on first run, cached after); a service-account bearer token is used
    instead when CRDB_MCP_API_KEY is set. Use as an async context manager:

        async with connect() as session:
            tools = await list_tools(session)
    """
    settings = get_settings()
    if not settings.crdb_mcp_url or not settings.crdb_mcp_cluster_id:
        raise RuntimeError(
            "CRDB_MCP_URL and CRDB_MCP_CLUSTER_ID must be set to connect to the CockroachDB Cloud MCP server"
        )

    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    # Bearer token → headless. Otherwise attach the OAuth provider as the httpx
    # auth so the SDK drives the consent/refresh flow on first use.
    auth = None if settings.crdb_mcp_api_key else _oauth_provider(settings)
    http_client = create_mcp_http_client(headers=_cluster_headers(settings), auth=auth)

    async with streamable_http_client(settings.crdb_mcp_url, http_client=http_client) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def list_tools(session) -> list[Any]:
    """Return the list of tools the MCP server advertises."""
    result = await session.list_tools()
    return result.tools


async def call_tool(session, name: str, args: dict[str, Any]) -> Any:
    """Call a named tool on the MCP server with the given arguments and return
    the raw CallToolResult (caller inspects .content / .is_error)."""
    return await session.call_tool(name, args)


# ── DB-overview helpers (read-only) ──────────────────────────────────────────
# The tools the DB-overview panel invokes. Deliberately a strict read-only
# allowlist — the server also exposes create_database/create_table/insert_rows,
# which felix never calls from this path.
OVERVIEW_TOOLS = ("get_cluster", "list_databases", "list_tables", "show_running_queries")


def _content_json(result: Any) -> Any:
    """Extract the first text content block of a CallToolResult and JSON-parse it.
    The CockroachDB MCP tools return their payload as a single JSON text block;
    fall back to the raw string if it isn't JSON, or None if there's no content."""
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return {"raw": text}
    return None


async def gather_overview(session, max_databases: int = 25) -> dict[str, Any]:
    """Assemble a read-only snapshot of the cluster for the DB-overview panel:
    cluster metadata, its databases, the tables in each, and any running queries.
    Uses only OVERVIEW_TOOLS. Per-database table listing is best-effort — a db
    that errors (e.g. no access) contributes an empty table list, not a failure."""
    cluster = _content_json(await call_tool(session, "get_cluster", {}))
    databases = (_content_json(await call_tool(session, "list_databases", {})) or {}).get("rows", [])

    tables_by_db: dict[str, list] = {}
    for db in databases[:max_databases]:
        name = db.get("database_name")
        if not name:
            continue
        try:
            listed = _content_json(await call_tool(session, "list_tables", {"database": name})) or {}
            tables_by_db[name] = listed.get("rows", [])
        except Exception:  # noqa: BLE001 - one db's failure shouldn't sink the whole overview
            tables_by_db[name] = []

    running = (_content_json(await call_tool(session, "show_running_queries", {})) or {}).get("rows", [])

    return {
        "cluster": cluster,
        "databases": databases,
        "tables_by_db": tables_by_db,
        "running_queries": running,
        "tools_used": list(OVERVIEW_TOOLS),
    }


def fetch_overview() -> dict[str, Any]:
    """Synchronous entry point for the (threadpool-run) API route: open a session,
    gather the overview, close it. Raises if the endpoint isn't configured or the
    connection/auth fails — the caller decides how to surface that."""
    import asyncio

    async def _run() -> dict[str, Any]:
        async with connect() as session:
            return await gather_overview(session)

    return asyncio.run(_run())


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        async with connect() as session:
            tools = await list_tools(session)
            print(f"Connected. {len(tools)} tool(s) available:")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")

    asyncio.run(_main())
