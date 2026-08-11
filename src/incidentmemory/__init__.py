"""incidentmemory — reusable core modules for felix (SRE incident-memory agent).

Submodules:
    embeddings   — swappable text embedder (Bedrock Titan V2 | local sentence-transformers)
    db           — CockroachDB connection, schema apply, insert helpers, vector recall, graph traversal
    mcp_client   — thin client for the CockroachDB Managed MCP Server

Nothing in this package touches the network or a model at import time — every external
client (boto3, the local embedding model, the DB connection, the MCP session) is created
lazily inside the function that needs it, so `import incidentmemory` is always safe even
without credentials or a live cluster.
"""
