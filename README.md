# Reflex — an SRE incident-memory agent

Built for the **CockroachDB × AWS "Build with Agentic Memory"** hackathon.

When production breaks, on-call engineers waste time rediscovering what the team already
learned: *have we seen this before, what caused it, what fixed it?* Reflex is an AI on-call
teammate whose value comes entirely from **memory**. For each incoming alert it recalls the
most relevant past incidents — matched by meaning, not keywords — to help diagnose and resolve
the current one, and writes every resolution back so it gets smarter over time.

**CockroachDB** is the memory layer: semantic recall over past incidents (native `VECTOR`
search), working memory for the active incident, and an append-only audit log — reached
through the Managed MCP Server. **AWS Bedrock** is the reasoning brain (Claude) and the
embedding model.

_Work in progress._
