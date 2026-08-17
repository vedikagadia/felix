# CLAUDE.md — felix

Orientation for anyone (or any agent) picking up this project. Read this first.

## What felix is

An **SRE / on-call incident-response agent whose value comes entirely from memory.**
For an incoming alert it recalls relevant past incidents (by *meaning*, not
keywords), pulls relevant docs and recent code changes, traces the code graph
from where the symptom surfaced up to where the cause likely lives, diagnoses,
and (eventually) writes the resolution back so it gets smarter over time.

Built for the **CockroachDB × AWS "Build with Agentic Memory"** hackathon
(deadline 2026-08-18). Requirements: use ≥2 CockroachDB tools and ≥1 AWS
service. The two CockroachDB offerings felix actually exercises on the live
path are **(1) the native VECTOR type + vector indexing** (semantic recall of
incidents/docs/changes — `embedding <-> %s::VECTOR(1024)` against
`*_embedding_idx`) and **(2) recursive-CTE graph traversal** (`WITH RECURSIVE`
over `code_edges` for the upstream symptom-origin trace). AWS: **Bedrock** —
Claude for reasoning (`clients/llm/bedrock.py`), Titan for embeddings
(`clients/embedder/titan.py`).

Beyond the vector type, felix now also exercises the **CockroachDB Cloud
Managed MCP Server** as a genuine, named second offering: `clients/cockroach_mcp.py`
is felix's *own* MCP client (the `mcp` Python SDK), authenticated to
`https://cockroachlabs.cloud/mcp` via **OAuth** (browser consent once, tokens
cached to `.crdb-mcp-tokens.json`, headless after) against the live `felix-db`
Cloud cluster (id in `CRDB_MCP_CLUSTER_ID`, sent as the `mcp-cluster-id`
header). The **DB overview** page (`GET /db/overview`) is powered entirely by
read-only MCP tool calls (`get_cluster` / `list_databases` / `list_tables` /
`show_running_queries`) — no direct SQL on that path. So the two named CockroachDB
offerings felix uses (vector indexing + the Managed MCP Server) are both
verified running; the recursive-CTE graph trace, the CDC changefeed, and the
**ccloud CLI (Agent-Ready)** — surfaced as a real interactive terminal in the
"CLI" panel (see below) — are additional CockroachDB capabilities on top, not
the counted offerings.

This is a **personal project** on the `vedikagadia` GitHub account. Commit as
Vedika Gadia / the vedikagadia noreply email. **Never commit under a Salesforce
identity.**

## The four memory sources (all in one CockroachDB)

| Source | Table(s) | Recall method | Rough share of problems it solves |
|---|---|---|---|
| Past incidents (episodic) | `incidents` + `resolution_steps` | vector search | ~50% |
| Project docs | `doc_chunks` | vector search | ~40% |
| Recent merges | `code_changes` | vector search **+ time window** | the "what changed?" signal |
| Code graph (structural) | `code_nodes` + `code_edges` | graph traversal (WITH RECURSIVE) | ~10% (code-only cases) |

Plus `agent_actions` (audit log of what the agent did) and `active_incidents`
+ `active_incident_turns` (working memory — the live multi-turn conversation an
incident is being triaged in; see "Multi-turn" below).

Recall = run the vector query against incidents + docs + (time-filtered)
changes, merge by distance in the app. The code graph is *traversed*, not
vector-searched.

## Repo layout

```
sql/
  schema.sql          # the 9 tables; VECTOR(1024) + vector indexes
  seed_dump.sql       # portable data-only dump (154 rows incl. embeddings), re-loadable
sample_project/
  checkout_service/   # the demo target service (Python; a realistic call graph + logs). payment_gateway.py
                      #   has a demo-only simulated round-trip so a real charge() genuinely takes time
  run.py              # traffic driver: CALLS CheckoutHandler.process in a loop with the timing probe
                      #   attached, so every checkout_latency_ms sample is measured, not fabricated
  seed/               # the authored memory corpora (fiction): incidents.json, docs.json, code_changes.json
  WORLD.md            # AUTHORITATIVE ground truth — every seed conforms to the names/logs/facts here
src/                  # layered: cli/api -> service -> clients/store -> models/config
  config.py           # Settings dataclass (reads .env once); swap env files, not code
  models.py           # domain dataclasses: Incident, DocChunk, CodeChange, CodeNode, GraphHit, Recall[T], EvidencePacket, Diagnosis, Message, AgentResponse (=Diagnosis|Message), DiagnosisResult
  cli.py / __main__.py# `python -m src {respond,seed,parse,mcp-probe,serve}` entry point
  clients/
    embedder/         # Embedder ABC + get_embedder(); titan.py (Bedrock), local.py (bge-large-en-v1.5). Both 1024-dim
    cockroach_mcp.py  # felix's own client for the CockroachDB Cloud MCP Server (OAuth + mcp-cluster-id header; powers `mcp-probe` and GET /db/overview)
  monitoring/         # Probe — reusable timing wrapper (@probe.timed / with probe.measure) that records
                      #   measured wall-clock latency into `metrics`; attach to any service (checkout is first)
  store/
    connection.py     # get_conn, apply_schema, vec_literal (VECTOR param helper)
    repositories/     # one per source: incidents, docs, changes, graph (blast_radius/upstream_callers), actions, active (working memory). Return domain models
  service/
    evidence_gatherer.py # EvidenceGatherer(conn, embedder) -> EvidencePacket (retrieval half of the loop)
    diagnoser.py      # IncidentDiagnoser: respond(session_id?) -> DiagnosisResult (reason + write-back, multi-turn aware); respond_stream() = SSE generator twin; diagnose() returns just the Diagnosis
  api/                # HTTP driver over the service layer (FastAPI): app.py (/chat, /chat/stream [SSE], /recall, /incidents, /incidents/search, /incidents/{id}/feedback, /metrics/config, /metrics/recent, /metrics/stream [SSE, CDC-backed], /db/overview + /db/plan + /db/execute [via CockroachDB MCP], /cli/status + /cli/ws [WebSocket PTY — see terminal.py], /alerts, /sessions/{id}, /health), schemas.py (serialize to the frontend contract), terminal.py (PTY-over-WebSocket bridge for the CLI panel)
  seed/
    parser.py         # AST -> code graph (42 nodes / 22 edges), deterministic uuid5 ids
    loader.py         # Seeder: parse + embed + insert -> the integration seam
docs/                 # GITIGNORED, local only — design docs + HTML architecture diagrams (not pushed)
frontend/             # React + Vite + TS chat UI (talks to src/api via POST /chat); mock mode when VITE_API_URL unset
```

## The two planted puzzles (the heart of the demo)

`WORLD.md` §5 defines two incidents, each solvable by **only one** memory source
— this is what proves each source pulls its weight:

- **Incident A — "code-only".** Symptom: `db.pool.exhausted` during traffic
  spikes; looks like a DB capacity problem; scaling the DB doesn't help. True
  cause: `CheckoutHandler.process` holds a pool connection across
  `PaymentClient.charge`'s retry loop, so slow gateway retries drain the pool.
  **No incident/doc/change reveals this** — only the code graph does (trace
  upstream from `ConnectionPool.acquire`, then read the culprit's source).
- **Incident B — "merge-only".** Symptom: customers report slow checkout but
  dashboards are green / no alert fired. True cause: a recent merge changed
  `metrics.py` `LATENCY_AGGREGATION` from `"p99"` to `"avg"`, hiding tail
  latency. **Only the `code_changes` record `chg-0001` reveals it** (and it's
  within the 14-day recall window). Notably `metrics.py` is NOT on the checkout
  call graph — so a *graph-scoped* change search would miss it; change recall is
  semantic + temporal, not graph-filtered. (See "design decisions" below.)

## How to run it (local dev)

**We test against a LOCAL single-node CockroachDB**, because the Cloud Basic
cluster is currently blocked on a 403 (org role/billing). Same wire protocol +
VECTOR type, so only `DATABASE_URL` changes — no code changes. When the Cloud
cluster is available, flip `DATABASE_URL` in `.env` and re-run the loader.

```bash
# 1. start the local node (VECTOR needs cockroach >= ~v24; v26.2.5 verified)
cockroach start-single-node --insecure --store=./.crdb-data \
  --listen-addr=localhost:26257 --http-addr=localhost:8080 --background
cockroach sql --insecure --host=localhost:26257 -e "CREATE DATABASE IF NOT EXISTS felix;"

# 2. python env
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt   # psycopg, python-dotenv, sentence-transformers, boto3, mcp, ...

# 3. config: copy .env.example -> .env, then for local use set:
#   DATABASE_URL=postgresql://root@localhost:26257/felix?sslmode=disable
#   EMBED_PROVIDER=local

# 4a. seed from scratch (parses code, embeds ~40 rows, inserts). First run
#     downloads the ~1.3GB bge-large model.
./.venv/bin/python -m src seed --apply-schema --truncate
# 4b. OR restore the committed dump instead of re-embedding:
cockroach sql --insecure --host=localhost:26257 --database=felix -f sql/schema.sql
cockroach sql --insecure --host=localhost:26257 --database=felix -f sql/seed_dump.sql

# 5. see the retrieval half in action
./.venv/bin/python -m src respond \
  "checkout failing, db.pool.exhausted during spike" --origin-node ConnectionPool.acquire

# 6. run the tests
./.venv/bin/python -m pytest              # deterministic: parsing + write-back. No API key/network.
./.venv/bin/python -m pytest -m live      # + the two planted-puzzle QUALITY checks (real Gemini + DB)
```

- SQL shell: `cockroach sql --insecure --host=localhost:26257 --database=felix`
  (list columns explicitly — don't `SELECT *`, the `embedding` column is 1024 floats).
- Web console: http://localhost:8080 (insecure = no login; local only).
- **Tests** (`tests/`): `test_parsing.py` (JSON extraction, citation-integrity
  guard, confidence clamp, step-shape coercion — no DB/network), `test_writeback.py`
  (FakeLLM + local DB; atomic write-back + no-orphan-on-failure, each test rolls
  back so the seed is untouched), `test_puzzles.py` (`-m live` only — diagnosis
  quality against the real model; a FakeLLM can't judge quality). The default
  run skips `live` and needs no API key. See `tests/conftest.py` for `FakeLLM`.

## Conventions & gotchas

- **`WORLD.md` is authoritative.** Any new seed data must use only the
  components / log lines / constants / code-symbol names defined there.
- **Two naming systems, kept distinct:** logical component names in
  incidents/docs (`checkout-handler`, `payment-gateway`, `connection-pool`, …)
  vs. real code-symbol names from the parser (`CheckoutHandler`,
  `PaymentClient.charge`, `ConnectionPool.acquire`, …). Don't conflate them.
- **Embeddings are computed at load time, never stored in the repo.** They
  depend on `EMBED_PROVIDER`; switching providers requires re-seeding
  (`loader.py --truncate`) so stored vectors match query vectors. `seed_dump.sql`
  contains the *local*-provider vectors.
- **`code_nodes.id` is deterministic** (uuid5 of `service:file:kind:qualname`),
  so re-syncing the graph UPSERTs in place instead of duplicating.
- **`code_changes.affected_components` is `STRING[]`** (code-node *names*), not
  resolved to node ids yet — that's a later graph-enrichment pass.
- **Gitignored / not pushed:** `docs/` (design docs + diagrams), `.env`,
  `.venv/`, `.crdb-data/` (the raw DB store — 1.3GB incl. a 1GB engine ballast
  file; use `sql/seed_dump.sql` for a portable snapshot instead).
- **VECTOR distance operator:** `db.py` uses `<->` (L2). If a cluster's index is
  built for cosine, switch to `<=>` (noted inline in `db.py`).

## Graph traversal — the direction matters

`code_edges` are directed in the **call direction** (`src` calls `dst`).
- `graph_blast_radius(name)` walks **downstream** (`src→dst`): "what does this
  node reach" = impact set. Use when `name` is the suspected *cause*.
- `graph_upstream_callers(name)` walks **upstream** (`dst→src`): "who reaches
  this node" = origin trace. Use when `name` is where a *symptom* surfaced and
  you need to find the real cause up the stack. This is the key primitive for
  the "the log fired low in the stack but originates elsewhere" case.

## Status (as of this writing)

Done & verified locally:
- schema, sample project + WORLD.md, all three seed corpora, parser, embedder,
  db helpers, loader, respond.
- End-to-end: seed load, semantic recall for **both** planted incidents,
  symptom-origin upstream graph trace. `seed_dump.sql` restores clean.
- **The reasoning step** (branch `llm-reasoning-layer`) — `clients/llm`
  (LLMClient ABC + `get_llm()`; Gemini default, Bedrock/Claude swappable) and
  `service/diagnoser.py` (`IncidentDiagnoser.respond` / `.diagnose`): recall →
  Option-B origin resolution → prompt → LLM → defensive parse + citation-integrity
  guard → atomic write-back (one transaction: minimal incident + resolution_steps
  + agent_actions). Both planted puzzles diagnose correctly end-to-end with live
  Gemini; verified by a 4-reviewer adversarial pass (diagnosis stability,
  negative control, citation integrity, `--no-llm` no-writes, no-orphan-rows).
- **The HTTP API** (`src/api/`, FastAPI) — a second thin driver over the service
  layer alongside the CLI. `python -m src serve` exposes `POST /chat` →
  `{response_type, diagnosis, message, evidence, session_id}` (a tagged union:
  `response_type` says whether this turn is a full `diagnosis` or a lightweight
  conversational `message` — the other field is null), `POST /chat/stream` → the same loop as
  **Server-Sent Events** (an `evidence` frame after recall, `delta` frames as the
  model generates, then a `done` frame with the full envelope — the frontend
  drives the live-reasoning UI off this), `POST /recall` → `{evidence}`
  (retrieval only), `GET /incidents?limit=` → `{incidents: [{item, distance:null}]}`
  (browse the whole episodic library, newest-first) and `GET /incidents/search?q=&k=`
  → `{query, incidents: [{item, distance}]}` (semantic search — embeds `q` and
  ranks by CockroachDB VECTOR distance, the incident-library page's showcase of
  vector recall), `POST /incidents/{id}/feedback` `{helpful: bool}` (the
  learning loop — see below), and `GET /health`. Serializes to the contract in
  `frontend/src/api/types.ts`. Requires `fastapi` + `uvicorn` (in
  requirements.txt). Verified end-to-end against the local node (both the
  blocking and streaming paths).
- **The frontend** (`frontend/`, React + Vite + TypeScript) — a chat UI: alert →
  diagnosis, with an evidence panel showing recalled incidents/docs/changes + the
  upstream trace (similarity bars, an expand/collapse call-trace, and
  bidirectional citation↔evidence highlighting). A second **Incident library**
  page (header nav) browses every past incident and semantic-searches them via
  `/incidents/search` (the vector-search showcase); each card's **Ask AI** button
  jumps back to Triage with the incident's symptoms pre-filled as a fresh
  conversation. Runs in mock mode with no backend (mock corpus + a keyword-overlap
  stand-in for vector distance); set `VITE_API_URL` (a `.env.local` pointing at
  `http://localhost:8000` is committed) to hit the API. `npm run build` +
  typecheck pass.

Done & verified (MCP):
- **CockroachDB Cloud Managed MCP Server — LIVE.** `python -m src mcp-probe`
  connects felix's own client to `https://cockroachlabs.cloud/mcp` (OAuth
  consent once, tokens cached) and lists 12 tools; `GET /db/overview` + the
  **DB overview** frontend page render a read-only cluster snapshot gathered
  purely through MCP tool calls, verified against the live `felix-db` cluster.
  This is the counted second CockroachDB offering.

Not yet built / deferred:
- **Bedrock model access** (Claude + Titan) — long-lead, needs the AWS console;
  until then `EMBED_PROVIDER=local`.
- **CockroachDB Cloud cluster** — the `felix-db` Basic cluster now exists (used
  live by the MCP path). `DATABASE_URL` still points at the local node for the
  recall/reasoning/CDC paths; flip it to Cloud when ready (the Cloud cluster
  already carries the seed tables).
- **Headless MCP auth** — the OAuth flow needs a one-time browser consent; a
  service-account bearer token (`CRDB_MCP_API_KEY`) would make it fully headless
  (the client already prefers it when set), if the org issues one.
- A considered-but-deferred idea: **graph-boosted change ranking** (union +
  boost changes that touch on-path files, rather than graph-*filtering* them —
  filtering would break Incident B). See `respond.py` discussion.

## Design decisions worth knowing

- Separate table per memory source (not one unified `memory_chunks`).
- `resolution_steps` as a child table (not JSONB) so steps are queryable across
  incidents.
- Code graph is a **current-state mirror** (deterministic ids, upsert-reconcile
  on re-sync), not versioned history.
- Embedder and (eventually) reasoning model are **swappable behind an env var**
  so DB/agent work isn't blocked on AWS approvals.
- `active_incident_turns` as a child table (not JSONB), mirroring
  `resolution_steps`, so the transcript stays queryable.

## Multi-turn / working memory

`active_incidents` + `active_incident_turns` are the working-memory tables felix
uses to hold an in-flight conversation, distinct from the episodic `incidents`
table. Flow (`IncidentDiagnoser.respond(alert, session_id=None)`):

- **First turn** (no `session_id`): recall → reason → write an episodic
  `incidents` row (as before) → open an `active_incidents` session linked to
  that incident and seed its transcript. Returns the new `session_id`.
- **Follow-up** (`session_id` set): the prior transcript is folded into the
  prompt so felix answers *in the context of the incident being triaged* (e.g.
  "did scaling the DB help?"). The exchange is recorded in working memory ONLY —
  **no new episodic incident per follow-up** (working vs. episodic memory).

`respond()`'s `active_repo` is optional: when it's not wired (the CLI, and the
write-back tests) the loop is single-turn and `session_id` stays `None` — so
existing callers are unchanged. The API (`POST /chat`) and the frontend thread
`session_id` through; the frontend's "＋ New incident" button clears it to start
a fresh conversation.

## Learning loop (feedback → recallable memory)

felix only *learns* from diagnoses a human confirms were good. This falls out of
the existing write-back: a live diagnosis is stored via `insert_minimal` (no
embedding → **invisible to recall**), so an unreviewed guess never pollutes
future retrieval. Human feedback promotes or discards it:

- **👍 helpful** (`POST /incidents/{id}/feedback {helpful:true}`): the endpoint
  embeds the incident's `title + symptoms` (the *same* text the seeder embeds,
  so it lands in the same vector subspace) and `UPDATE`s the row's `embedding` +
  sets `feedback = 'helpful'`. The incident is now recallable — a future similar
  alert will surface it. It also starts appearing in the library's vector search
  with a **✓ confirmed** badge.
- **👎 not helpful** (`{helpful:false}`): sets `feedback = 'not_helpful'` and
  clears the embedding again, so a diagnosis judged wrong is never recalled.

`incidents.feedback` (`'helpful' | 'not_helpful' | NULL`) is the new column
(idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, mirroring
`active_incidents.source`). Every vote is also written to `agent_actions`
(`action_type='feedback'`). Repo primitive: `IncidentRepository.record_feedback`.
The frontend renders 👍/👎 on each `DiagnosisCard` (keyed on
`diagnosis.incident_id`); mock mode no-ops the call. Seed incidents already carry
embeddings and a NULL feedback, so they stay recallable exactly as before — the
gate only applies to live-diagnosed rows.

## DB overview (via the CockroachDB Cloud Managed MCP Server)

A read-only view of the live cluster, powered end-to-end by MCP — the concrete
demonstration that felix *uses* the Managed MCP Server (not just Claude Code).

- **felix's client** (`src/clients/cockroach_mcp.py`): the `mcp` Python SDK
  talking to `https://cockroachlabs.cloud/mcp`. Sends the required
  `mcp-cluster-id` header. Auth is **OAuth** by default — first `connect()`
  opens a browser consent (a one-shot localhost callback captures the code),
  then tokens are cached to `.crdb-mcp-tokens.json` (gitignored) and reused
  headlessly; a service-account `CRDB_MCP_API_KEY` bearer token is used instead
  when set. `connect()` / `list_tools()` / `call_tool()` are the primitives;
  `gather_overview()` + the sync `fetch_overview()` assemble the snapshot from a
  strict **read-only allowlist** (`OVERVIEW_TOOLS` — never create/insert).
- **The endpoint** (`GET /db/overview`): calls `fetch_overview()` and returns
  `{connected, source, cluster, databases, tables_by_db, running_queries,
  tools_used}`. Degrades to `{connected:false, reason}` (HTTP 200) when MCP
  isn't configured or auth/connection fails, so the panel renders a soft state
  rather than 500ing. Talks to the cluster purely over MCP — no `DATABASE_URL`
  connection on this path.
- **The panel** (`frontend/src/components/DbOverviewPage.tsx`, "DB overview"
  nav tab): a cluster card (name / version / provider / plan / regions + the
  MCP tools invoked), per-database table lists with estimated row-count bars,
  and a live running-queries card. Seam is `frontend/src/api/db.ts` (mock mode
  synthesizes the `felix-db` snapshot so it's demoable offline).
- **`python -m src mcp-probe`** connects + lists the server's tools (12 as of
  writing: get_cluster, list/create databases + tables, select_query,
  insert_rows, explain_query, show_running_queries, show_statement, …). Run it
  once to complete the OAuth consent that seeds the token cache.

### Natural-language DB writes ("Ask felix to change the DB")

The DB-overview panel also has a text box that turns a plain-English request
("add a table for on-call schedules") into a real CockroachDB write, executed
over MCP — **preview-then-confirm**, so nothing runs until the operator OKs the
exact tool call.

- **The planner** (`src/service/db_assistant.py`, `plan_operation(llm,
  instruction)`): felix's LLM has no native tool-calling, so this prompts it (with
  the tool menu from `cockroach_mcp.NL_TOOL_CATALOG`) to emit strict JSON, parses
  it defensively (fenced block → outermost `{...}`, never raises — mirrors the
  diagnoser's `_extract_json_object`), validates the chosen tool against
  `NL_TOOLS`, and coerces the value into the single arg key that tool expects.
  Returns a reviewable `{tool, args, explanation, write}` or `{tool:null, reason}`.
- **The allowlist** (`cockroach_mcp.NL_TOOL_CATALOG` / `NL_TOOLS`): only
  `create_table` / `create_database` / `insert_rows` (writes) + `select_query`
  (read). The server exposes **no** drop/truncate/update/delete tool, so every
  write is **additive** — worst case is a new table or extra rows. Arg contract
  discovered by probing live (the key differs per tool): `create_table` takes
  `{ddl}`, `insert_rows`/`select_query` take `{query}`, `create_database` takes
  `{name}`. The MCP session's current database is `system` (not writable by the
  `managed-mcp` user), so the planner is told to **fully-qualify** every table as
  `defaultdb.public.<name>` and to emit `CREATE TABLE IF NOT EXISTS` (idempotent,
  so a retry after a transient server "Internal error" is safe). `run_tool`
  unwraps anyio `TaskGroup` ExceptionGroups so the operator sees the real MCP
  error, not "unhandled errors in a TaskGroup".
- **The endpoints**: `POST /db/plan {instruction}` → `{plan}` or `{plan:null,
  reason}` (maps only; **never executes**; 503 if LLM or MCP unconfigured).
  `POST /db/execute {tool, args}` → re-validates `tool ∈ NL_TOOLS` (403 otherwise)
  then `cockroach_mcp.run_tool` runs it over MCP, returning `{ok, tool, args,
  result|error}` (tool-level failure stays HTTP 200 so the panel can show it).
- **The UI** (`DbWriteBox` in `DbOverviewPage.tsx`): textarea → **Plan it** →
  previews the tool + SQL + explanation → **Run it** / **Cancel** → shows the raw
  MCP result and refreshes the overview so a new table/rows appear. Seam funcs
  `planDbOperation` / `executeDbOperation` in `api/db.ts` (mock mode does a
  keyword stand-in for the planner and a no-op execute).

## CLI panel (the interactive ccloud terminal)

felix's third named CockroachDB offering made tangible: a **real interactive
terminal** in the UI wired to the **ccloud CLI (Agent-Ready)**. The backend
spawns a login shell in a PTY with `ccloud` on PATH and bridges it to an
xterm.js terminal in the browser, so `ccloud cluster list`, `ccloud cluster sql
felix-db`, etc. run for real against the authed Cloud account.

- **The PTY bridge** (`src/api/terminal.py`, `run_terminal(ws)`): `pty.fork()`s a
  login shell (`$SHELL`, overridable via `FELIX_CLI_SHELL`), sets `TERM=xterm-256color`,
  and bridges the master fd ↔ WebSocket. Client→server frames are JSON
  (`{"type":"input","data"}` keystrokes, `{"type":"resize","cols","rows"}` →
  `TIOCSWINSZ`); server→client frames are raw output bytes. A single queue-drained
  sender preserves byte order; on either side closing it removes the reader,
  SIGHUPs + reaps the shell, and closes the socket.
- **Security**: this is a FULL SHELL over the socket (the operator explicitly
  chose the interactive-terminal option), i.e. effectively RCE. It's gated behind
  `FELIX_CLI_ENABLED` (Settings.cli_enabled, default true) and the API binds
  `127.0.0.1` by default (`serve`). **Never expose this on a public interface.**
- **The endpoints**: `WS /cli/ws` (the terminal) and `GET /cli/status` →
  `{enabled, ccloud_installed, ccloud_path, account, cluster_id}` (introspection
  for the panel banner — runs `ccloud auth whoami`; spawns no shell).
- **The panel** (`frontend/src/components/CliPage.tsx`, "CLI" nav tab): an
  xterm.js terminal (+ FitAddon, ResizeObserver-driven resize) over a native
  `WebSocket`; a status banner shows connection state + which Cloud account
  ccloud is authed as. Seam is `frontend/src/api/cli.ts` (`cliWsUrl()` derives
  ws(s):// from `VITE_API_URL`; `fetchCliStatus()`); no meaningful mock — a
  terminal needs a real host, so mock mode renders an explanatory placeholder.
  Requires `@xterm/xterm` + `@xterm/addon-fit` (added to frontend deps).
- **Prereq**: `brew install cockroachdb/tap/ccloud` then `ccloud auth login`
  (browser, one-time) — the backend inherits that session.

## Live monitoring (CDC — observe services in real time)

A **reusable timing wrapper** + a **live panel** that watches instrumented
services off a CockroachDB changefeed. Distinct from the `watcher.py` anomaly
path (which trips → auto-triages): this is the *observe-it-live* view.

- **The probe** (`src/monitoring/probe.py`): `Probe.for_repo(MetricRepository)`
  gives `@probe.timed(service, metric)` (decorator) and `with probe.measure(...)`
  (context-manager) — measures wall-clock latency (ms) and writes one `metrics`
  row per call, labelled `{"ok": bool}`. Generic: attach to any callable/service.
- **The sample service** is genuinely instrumented: `sample_project/run.py` now
  *calls* `CheckoutHandler.process` in a loop with the probe attached (no more
  fabricated numbers). It wraps **three** callables — one card each on the panel:
  `checkout-service`/`checkout_latency_ms` (whole request),
  `payment-gateway`/`payment_latency_ms` (where the spike originates), and
  `fulfillment`/`enqueue_latency_ms` (fast → a healthy card). `payment_gateway.py`
  sleeps a simulated round-trip and degrades every `GATEWAY_SLOW_EVERY`-th charge
  into exactly one real retry/backoff — a bounded ~1s tail spike over a
  ~100ms/mean<300ms baseline (the avg-hides-the-tail shape). Verified: checkout
  & payment p99≈1.2s, mean≈150–180ms; enqueue ≈0ms.
- **The stream**: `GET /metrics/stream` holds a sinkless
  `EXPERIMENTAL CHANGEFEED FOR metrics` and relays each new row as an SSE
  `sample` frame (its OWN connection — a changefeed portal can't be shared).
  `GET /metrics/recent` backfills history. Note: `/metrics/stream` opens a
  connection per subscriber for the demo's scale.
- **The panel** (`frontend/src/components/LiveMonitoringPage.tsx`, "Live
  monitoring" nav tab): seeds sparklines from `/metrics/recent`, tails
  `/metrics/stream` via native `EventSource` (a plain GET SSE — no hand-rolled
  parser, unlike `/chat/stream`), and renders one card per `(service, metric)`
  **automatically** — a live value, sparkline w/ threshold line, rolling p99/avg,
  and a **⚠ tail latency high** badge when p99 ≥ the card's alert level. The
  stream seam lives in `frontend/src/api/metrics.ts` (mock mode synthesizes the
  same shape). When a card is red it also shows an **Ask felix →** button that
  jumps to Triage with a synthesized alert pre-filled from the card's live
  p99/avg numbers (the same jump-to-Triage flow as the library's "Ask AI", via
  `App.askFelixAbout`) — one click from "a card went red" to triaging the spike.
- **Configurable alert levels**: `GET /metrics/config` serves the default p99
  thresholds from `Settings` (`METRIC_ALERT_DEFAULT_P99_MS` global default +
  `METRIC_ALERT_THRESHOLDS` per-metric JSON, both in `.env`). The panel seeds
  each card from that, and **every card's alert level is editable live** (a p99
  number input); an override persists in `localStorage` (`felix:alert:<svc>::<metric>`)
  and can be reset back to the configured default. So the trip threshold is
  settable both by ops (env) and by the operator (UI).
- **Caveat**: the sample fulfillment queue never drains (no worker), so a
  *very* long `run.py` session eventually hits `QueueFull` at `QUEUE_MAX_DEPTH`
  (5000) — the driver catches it, logs, and still emits the latency sample. Fine
  for demo-length runs (minutes).
