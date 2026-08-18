# 🦊 felix — an SRE incident-memory agent

> An on-call teammate whose value comes **entirely from memory**. For each alert
> it recalls the relevant past incidents, docs, code changes, runbooks, and code
> structure — matched by *meaning*, not keywords — traces the symptom to its
> likely cause, diagnoses, and writes the resolution back so it gets smarter over
> time.

Built for the **CockroachDB × AWS "Build with Agentic Memory"** hackathon. All
memory lives in **one CockroachDB** (native `VECTOR` search, recursive-CTE graph
traversal, and a live CDC changefeed); the reasoning brain and embeddings run on
**AWS Bedrock** (swappable to local models for dev).

- 📖 **Setup / run it yourself:** [SETUP.md](SETUP.md)
- 🏗️ **Code walkthrough (`src/`):** [SRC_OVERVIEW.md](SRC_OVERVIEW.md)
- 🧭 **Deep design notes / conventions:** [CLAUDE.md](CLAUDE.md)

---

## The problem

When production breaks, on-call engineers waste time rediscovering what the team
already learned: *have we seen this before? what caused it? what fixed it?* That
knowledge is scattered across old incidents, wikis, merge history, dashboards,
and the code itself. felix makes that knowledge an agent's memory — and proves
that **memory, not the model, is what makes the agent useful**.

## Why it's convincing: two planted puzzles

felix ships with a fictional-but-realistic target service and a curated memory
corpus (`sample_project/`, ground truth in `WORLD.md`) containing two incidents
each solvable by **only one** memory source — so every source visibly earns its
place:

| Puzzle | Symptom | Only-source that cracks it |
|---|---|---|
| **A — "code-only"** | `db.pool.exhausted` during spikes; scaling the DB doesn't help | The **code graph** — trace upstream from `ConnectionPool.acquire` and find `CheckoutHandler.process` holding a pool connection across a slow retry loop. No incident/doc/change reveals it. |
| **B — "merge-only"** | Customers report slow checkout but **dashboards are green** | A single **`code_changes` merge** that flipped `LATENCY_AGGREGATION` from `p99` → `avg`, hiding the tail. Only the time-windowed change recall surfaces it. |

---

## Feature showcase

felix is a chat-first web app (React + Vite + TS) over a FastAPI backend, with
**five panels**:

### 1. 🩺 Triage
Alert → diagnosis. Recall runs first and fills an **evidence panel**
(incidents, docs, changes, runbooks, upstream code trace, and breached
downstream health checks), then the model streams its reasoning over
**Server-Sent Events**. Extras:
- a **reasoning-replay overlay** that animates recall → reasoning for a fresh incident;
- **bidirectional citation ↔ evidence highlighting** (hover a citation, the source card lights up);
- **multi-turn** follow-ups in the same conversation (working memory), and a
  tagged-union response so felix can reply with a full *diagnosis* or a
  lightweight conversational *message* per turn.

### 2. 📚 Incident library
Browse the whole episodic library, or **semantic-search** it — the query is
embedded and ranked by CockroachDB `VECTOR` distance (the vector-recall
showcase). Every card has **Ask AI**, which jumps to Triage pre-filled with that
incident's symptoms. Confirmed diagnoses show a **✓ confirmed** badge.

### 3. 📈 Live monitoring (CDC)
Live latency cards fed by a **sinkless CockroachDB changefeed** on the `metrics`
table (streamed to the browser as SSE). Each card auto-renders a live value,
sparkline, rolling **p99 / avg**, and a **⚠ tail-latency-high** badge when p99
crosses its (editable) alert threshold. When a card goes red, **Ask felix →**
jumps to Triage with a synthesized alert pre-filled.

### 4. 🗄️ DB overview (via the Managed MCP Server)
A read-only cluster snapshot — cluster metadata, databases, tables + row counts,
running queries — gathered **entirely through the CockroachDB Cloud Managed MCP
Server** (felix as its *own* OAuth MCP client; no direct SQL on this path). Plus
**"Ask felix to change the DB"**: a natural-language box that maps a request
("add a table for on-call schedules") to one MCP tool call, **previews it, and
runs it only after you confirm** — additive writes only (create/insert).

### 5. 🖥️ CLI (the ccloud terminal)
A **real interactive terminal** in the browser (xterm.js) bridged to a PTY on
the backend with the **ccloud CLI** on PATH — so `ccloud cluster list`,
`ccloud cluster sql felix-db`, etc. run for real against the authed Cloud
account. Gated behind `FELIX_CLI_ENABLED`; the API binds `127.0.0.1` (local demo
only — it's a real shell).

### And a headless real-time loop
`python -m src watch` holds the same changefeed, trips on the "p99 spikes while
avg stays flat" signature, and hands a synthesized alert to the diagnoser
automatically — the "no alert fired, but customers are complaining" story,
end to end. (See [SETUP.md §7](SETUP.md).)

---

## The memory (all in one CockroachDB)

| Source | Table(s) | How it's recalled |
|---|---|---|
| Past incidents (episodic) | `incidents` + `resolution_steps` | vector search |
| Project docs | `doc_chunks` | vector search |
| Recent merges | `code_changes` | vector search **+ time window** |
| Curated runbooks | `runbooks` + `runbook_steps` | vector search (by trigger text) |
| Code graph (structural) | `code_nodes` + `code_edges` | recursive-CTE traversal |
| Service topology + live metrics | `service_nodes` + `service_edges` + `metrics` | recursive-CTE downstream walk → live metric health checks |

Plus **working memory** (`active_incidents` + `active_incident_turns` — the live
multi-turn conversation) and an **audit log** (`agent_actions`).

**The reasoning loop:** recall → origin resolution → prompt → LLM → defensive
parse + **citation-integrity guard** → **atomic write-back** (one transaction:
incident + steps + audit). felix only *learns* from diagnoses a human confirms:
a live diagnosis is stored **without an embedding** (invisible to recall) until a
👍 promotes it into recallable memory (👎 keeps it out) — so an unreviewed guess
never pollutes future retrieval.

---

## How felix maps to the judging criteria

### 🧠 Agentic Memory Design — *is CockroachDB a meaningful, production-grade memory layer?*
CockroachDB **is** the agent — take the database away and felix has nothing to
reason over. Memory is deliberately multi-modal, not one embeddings table: six
sources (episodic incidents, docs, merges, runbooks, the code graph, and service
topology) recalled by **three genuinely different mechanisms** — native `VECTOR`
similarity, recursive-CTE graph traversal, and a CDC changefeed — plus
**transactional working memory** (`active_incidents` + turns) and an **audit
log** (`agent_actions`). Write-back is a single atomic transaction, and the
**learning loop** uses the DB's own state (embedding present/absent) as the gate
for what becomes recallable. The two planted puzzles *prove* each source is
load-bearing rather than decorative.

### 🛠️ Technical Implementation — *correct, safe use of the CockroachDB tools*

**≥ 2 CockroachDB tools** (both verified running live):

| Tool | How felix uses it | Correctness / safety |
|---|---|---|
| **Distributed Vector Indexing** | Every recall is `embedding <-> %s::VECTOR(1024)` against `*_embedding_idx`; query & stored vectors always share a provider so the space matches. | 1024-dim enforced end to end; re-seeding on provider change is documented so vectors never drift. |
| **Cloud Managed MCP Server** | felix's *own* `mcp`-SDK client (OAuth, `mcp-cluster-id` header) powers the read-only DB-overview and NL DB writes. | Writes go through an **additive-only allowlist** (create/insert — the server exposes no drop/truncate/update/delete) with **preview-then-confirm**; every table is fully-qualified and `IF NOT EXISTS` so a retry is idempotent. |

**Bonus CockroachDB capabilities on top:** recursive-CTE graph traversal (code
graph + service topology), the CDC changefeed (live monitoring + watcher), and
the **ccloud CLI (Agent-Ready)** surfaced as the in-app terminal.

**≥ 1 AWS service:** **Amazon Bedrock** — Claude for reasoning
(`clients/llm/bedrock.py`), Titan for embeddings (`clients/embedder/titan.py`).
Both are swappable behind an env var with local fallbacks (Gemini +
`bge-large-en-v1.5`), so the DB/agent work is never blocked on cloud approvals.
The reasoning path is hardened: **defensive JSON parsing** (never raises), a
**citation-integrity guard** (the model can't cite evidence it wasn't given),
and a confidence clamp.

### 🌍 Real-World Impact — *does it matter to real workflows?*
On-call engineers routinely re-solve incidents the team already solved, because
that knowledge is scattered across old tickets, wikis, merge history, dashboards,
and the code itself. felix turns all of it into one recallable memory and traces
a symptom to its cause — the exact work that eats an SRE's night. The thesis
("memory, not the model, is what makes the agent useful") generalizes to any
domain with hard-won institutional knowledge.

### 🛡️ Production Readiness — *secure, observable, resilient, scalable?*
See the [dedicated section below](#security--production-readiness). In short:
additive-only DB writes with human confirm, soft-degradation when MCP is
unreachable, idempotent retries, localhost-bound API, OAuth tokens cached out of
the repo, and self-observability via the timing probe + CDC metrics. Known
limitations are stated, not hidden.

### 💡 Creativity & Originality — *a genuinely new idea?*
An agent whose **entire value is its memory**, validated by **planted puzzles**
that each isolate one source; root-cause discovery by **graph traversal** rather
than similarity alone; a "green dashboards, angry customers" puzzle that only a
*merge* record can crack; and a learning loop that only promotes **human-confirmed**
diagnoses into recall. These lean into what makes agentic systems different from
traditional apps — memory that compounds — rather than bolting an LLM onto CRUD.

---

## Security & production readiness

felix is a hackathon demo, but it was built with the "what happens when things go
wrong?" question in mind. What's already in place:

- **Least-privilege, additive-only DB writes.** The NL-write path can only reach
  a 4-tool allowlist (`create_table` / `create_database` / `insert_rows` /
  `select_query`); the MCP server exposes no destructive tool, so the worst case
  is a new table or extra rows — and nothing runs until the operator confirms the
  exact tool call in a preview.
- **Graceful degradation.** `GET /db/overview` returns `{connected:false, reason}`
  (HTTP 200) when MCP is unconfigured or auth fails, so the UI shows a soft state
  instead of a 500. MCP `TaskGroup` errors are unwrapped to the real message.
- **Idempotent recovery.** Planned DDL is emitted as `CREATE ... IF NOT EXISTS`,
  so retrying after a transient server error is safe.
- **Secrets stay out of the repo.** OAuth tokens are cached to
  `.crdb-mcp-tokens.json` (gitignored); `.env` is gitignored; providers are
  configured by env var.
- **Local-only blast radius by default.** The API binds `127.0.0.1`; the
  interactive terminal is a **real login shell over WebSocket (effectively RCE)**
  and is therefore gated behind `FELIX_CLI_ENABLED` and **must never be exposed
  on a public interface**.
- **Self-observability.** The reusable timing `Probe` records measured wall-clock
  latency into a `metrics` table; the CDC changefeed streams it live and the
  `watch` loop trips on anomalies — felix can watch the very services it triages.

**Known limitations (honest list).** The HTTP API has no authentication layer
yet (localhost binding is the current control); `/metrics/stream` opens one DB
connection per subscriber (fine at demo scale); the reasoning model has no
native tool-calling, so NL→SQL is prompt-to-JSON with defensive parsing rather
than a formal grammar; and the demo runs against a local single-node cluster.

**Scale path.** Nothing on the hot path is architecturally single-node:
CockroachDB's distributed vector index and recursive-CTE traversal scale
horizontally, the changefeed is the natural scale-out ingestion primitive, and
memory is partitioned by source table (each independently indexed). Moving from
the local node to the managed Cloud cluster is a single `DATABASE_URL` change —
no code changes — because the same wire protocol and `VECTOR` type back both.

---

## Architecture

```
                     React + Vite + TS  (5 panels)
                              │  REST + SSE + WebSocket
                              ▼
   FastAPI  (src/api)  ── /chat /chat/stream /recall /incidents /metrics/*
                          /db/overview /db/plan /db/execute  /cli/ws  ...
                              │
   Service layer (src/service) ── EvidenceGatherer · IncidentDiagnoser ·
        TopologyHealthService · MetricQueryBuilder · DbAssistant · MetricWatcher
                              │
   Store (src/store) ── repositories (one per source) · connection · VECTOR helper
                              │
        ┌─────────────────────┴───────────────────────┐
        ▼                                              ▼
   CockroachDB                                    AWS Bedrock
   (VECTOR · recursive CTE · CDC · MCP)           (Claude · Titan)
```

Layered: `cli/api → service → store/clients → models/config`. Clients (embedder,
LLM, MCP) are lazy-imported and swappable behind env vars. Full walkthrough in
[SRC_OVERVIEW.md](SRC_OVERVIEW.md).

---

## Quick start

Full instructions (Docker or native, seeding, the CDC demo, troubleshooting) are
in **[SETUP.md](SETUP.md)**. The short version:

```bash
# 1. A local CockroachDB (VECTOR needs cockroach >= v24; v26.2.5 verified)
cockroach start-single-node --insecure --store=./.crdb-data \
  --listen-addr=localhost:26257 --http-addr=localhost:8080 --background
cockroach sql --insecure -e "CREATE DATABASE IF NOT EXISTS felix;"

# 2. Python env
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 3. Config: cp .env.example .env, then for local dev set:
#    DATABASE_URL=postgresql://root@localhost:26257/felix?sslmode=disable
#    EMBED_PROVIDER=local     LLM_PROVIDER=gemini (set GEMINI_API_KEY)

# 4. Seed memory (or restore the committed dump — see SETUP.md §4)
./.venv/bin/python -m src seed --apply-schema --truncate

# 5a. See recall from the CLI (no server, no LLM):
./.venv/bin/python -m src respond \
  "checkout failing, db.pool.exhausted during spike" --origin-node ConnectionPool.acquire

# 5b. Or run the full app:
./.venv/bin/python -m src serve            # backend on :8000
cd frontend && npm install && npm run dev  # UI on :5173 (VITE_API_URL → :8000)
```

The frontend runs in **mock mode** with no backend (`VITE_API_URL` unset), so the
UI is demoable offline. The **CLI panel** additionally needs the ccloud CLI:
`brew install cockroachdb/tap/ccloud && ccloud auth login`.

### CLI entry points

```
python -m src respond "<alert>" [--origin-node NAME] [--no-llm]   # recall (+diagnose)
python -m src serve [--reload]                                    # the HTTP API
python -m src watch [--debug]                                     # CDC anomaly loop
python -m src seed  [--apply-schema] [--truncate]                 # load memory
python -m src parse                                               # code → graph summary
python -m src mcp-probe                                           # connect + list MCP tools
```

---

## Repo layout

```
sql/            schema.sql (the tables; VECTOR + indexes) · seed_dump.sql (portable dump)
sample_project/ the demo target service + authored memory corpora + WORLD.md (ground truth)
src/            config · models · cli · api/ · service/ · store/ · clients/ · monitoring/ · seed/
frontend/       React + Vite + TS UI (5 panels; mock mode when VITE_API_URL unset)
tests/          deterministic (parsing + write-back) + opt-in live puzzle-quality checks
```

## Tests

```bash
./.venv/bin/python -m pytest            # deterministic: parsing + write-back. No API key/network.
./.venv/bin/python -m pytest -m live    # + planted-puzzle diagnosis-quality checks (real model + DB)
```

---

## Status

Verified end to end against a local CockroachDB node: schema + seed, semantic
recall for both planted puzzles, the upstream graph trace, the full
recall→reason→write-back loop (live Gemini), the HTTP API (blocking + streaming),
the CDC live-monitoring + watcher paths, the frontend, the **Managed MCP Server**
paths (DB overview + NL writes, live OAuth), and the **ccloud CLI** terminal.
Bedrock (Claude + Titan) is wired and swappable behind env vars; local providers
are the default for dev. See [CLAUDE.md](CLAUDE.md) for the detailed status and
design decisions.
