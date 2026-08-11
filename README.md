# Felix — an SRE incident-memory agent

Built for the **CockroachDB × AWS "Build with Agentic Memory"** hackathon.

When production breaks, on-call engineers waste time rediscovering what the team already
learned: *have we seen this before, what caused it, what fixed it?* Felix is an AI on-call
teammate whose value comes entirely from **memory**. For each incoming alert it recalls the
most relevant knowledge — matched by meaning, not keywords — to help diagnose and resolve the
incident, and writes every resolution back so it gets smarter over time.

Felix draws on three kinds of memory, all backed by **CockroachDB**:
- **Past incidents** — semantic recall over symptom→cause→fix records (native `VECTOR` search).
- **Project documentation** — how the system works and how it's run, chunked and searched semantically.
- **Code structure** — a live graph of the codebase for the incidents that only the code can explain.

Memory is reached through the CockroachDB **Managed MCP Server**. **AWS Bedrock** is the
reasoning brain (Claude) and the embedding model.

_Work in progress._
