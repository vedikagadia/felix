"""Thin client for the CockroachDB Managed MCP Server.

Scaffolding for the discovery spike (spikes/03_mcp.py) that confirms what
tools the Managed MCP Server exposes and how vector recall gets routed
through it (see docs/ARCHITECTURE.md, "The dual-path decision"). Nothing here
connects at import time — connect()/list_tools()/call_tool() are the only
functions that touch the network, so `import incidentmemory.mcp_client` is
always safe without CRDB_MCP_URL / CRDB_MCP_API_KEY set.

Auth: the Cloud Console's MCP config snippet for CockroachDB typically wants
the API key sent as a bearer token (Authorization: Bearer <key>) over the
Streamable HTTP transport. That's what's wired up below.

  NOTE: the exact auth header shape (Authorization: Bearer vs. a custom
  header like X-Cockroach-Api-Key) and the transport (streamable HTTP vs.
  SSE) are asserted here from the Cloud Console docs, not yet verified
  against a live endpoint — this is the single line most likely to need
  adjustment once CRDB_MCP_URL/CRDB_MCP_API_KEY are live. See connect() below.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


@asynccontextmanager
async def connect():
    """Open an MCP ClientSession against CRDB_MCP_URL, authenticated with
    CRDB_MCP_API_KEY. Use as an async context manager:

        async with connect() as session:
            tools = await list_tools(session)

    Yields an initialized ClientSession.
    """
    load_dotenv()
    url = os.environ["CRDB_MCP_URL"]
    api_key = os.environ["CRDB_MCP_API_KEY"]

    # NOTE: adjust this header if the Cloud Console's MCP config snippet specifies
    # a different auth scheme (e.g. a custom "X-Cockroach-Api-Key" header) — Bearer
    # is the common convention but hasn't been confirmed against the live endpoint.
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {api_key}"})

    async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def list_tools(session: ClientSession) -> list[Any]:
    """Return the list of tools the MCP server advertises."""
    result = await session.list_tools()
    return result.tools


async def call_tool(session: ClientSession, name: str, args: dict[str, Any]) -> Any:
    """Call a named tool on the MCP server with the given arguments and return
    the raw CallToolResult (caller inspects .content / .isError)."""
    return await session.call_tool(name, args)


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        async with connect() as session:
            tools = await list_tools(session)
            print(f"Connected. {len(tools)} tool(s) available:")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")

    asyncio.run(_main())
