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

Challenge
The Challenge
Build an agentic application that uses CockroachDB as its persistent memory layer, deployed on AWS.

Your agent should store, retrieve, and act on memory whether that's conversation history, user context, task state, embeddings, or structured transactional data. The best submissions will demonstrate that memory is not an afterthought, it is the thing that makes an agent useful in production.

All submissions must use at least two of the following CockroachDB tools:

CockroachDB Cloud Managed MCP Server — Connect AI agents directly to CockroachDB clusters with a single config snippet from the Cloud Console. Works natively with Claude Code, Cursor, and VS Code. Safe by default: read-only mode, full audit logging, zero custom proxy required. Endpoint: https://cockroachlabs.cloud/mcp
CockroachDB Distributed Vector Indexing — Store and query embeddings at scale using CockroachDB's vector support with distributed indexing. Semantic search and retrieval stay fast as your data grows — no separate vector store to maintain, no reindexing pain, and no consistency gaps between your vector data and your operational database. Ideal for RAG pipelines, long-term agent memory, and semantic search applications.
ccloud CLI (Agent-Ready) — Give your agent direct, secure access to the full CockroachDB Cloud control plane. Provision clusters, manage backups, configure networking, monitor audit logs — all from the terminal. Designed for AI with consistent noun-verb patterns, JSON output on every command, and granular service-account-based RBAC.
CockroachDB Agent Skills Repo (Open Source) — A curated, open-source collection of machine-executable Agent Skills encoding CockroachDB expertise. Skills span onboarding, query/schema design, operations, performance, security, and observability. Portable across Claude, Cursor, LangChain, and any MCP-compatible client.
All submissions must also use at least one AWS service:

Amazon Bedrock (foundation models, knowledge bases, or agents)
AWS Lambda (serverless agent execution)
Amazon ECS / EKS (containerized agent workloads)
Amazon S3 (artifact or document storage)
Amazon SageMaker (model training or inference)
Amazon Bedrock Agents (multi-step agentic workflows)
Any other AWS service that powers your agent's environment