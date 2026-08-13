# felix — Setup Guide

How to stand up the whole felix stack locally: **CockroachDB** (memory store) →
**Python backend** (recall + LLM reasoning, CLI *and* HTTP API) → **React
frontend** (chat UI). This is the practical, step-by-step companion to
`CLAUDE.md` (the "what/why") and `SRC_OVERVIEW.md` (the code tour).

By the end you'll have:

- a single-node CockroachDB with the 9 tables and 154 seeded rows (incidents,
  docs, code changes, code graph — embeddings included; the two working-memory
  tables populate at runtime as you chat),
- the API at `http://localhost:8000` (`/health`, `/recall`, `/chat`),
- the frontend at `http://localhost:5173` talking to that API.

---

## 0. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| CockroachDB | **≥ v24.3** (v26.2.5 verified) | Needs the native `VECTOR` type + vector indexes. Docker or native binary — see step 2. |
| Python | **3.12** (3.10+ works) | The venv below was verified on 3.12.13. |
| Node.js | **18+** | For the Vite/React frontend. |
| Docker | any recent | Only if you take the Docker path for CockroachDB. |

You also need a **Gemini API key** for the reasoning step (`/chat`). The
retrieval-only path (`/recall`, `--no-llm`) works without it. Get one at
<https://aistudio.google.com/apikey>. (Bedrock/Claude is the swappable
alternative but its access is deferred — leave `LLM_PROVIDER=gemini`.)

```bash
cd /path/to/felix
```

---

## 1. CockroachDB

Pick **one** of the two options.

### Option A — Docker (recommended; easy + persistent)

The repo ships a `docker-compose.yml` that starts a single node, mounts a
**persistent named volume**, and runs `docker/init-db.sh` once to create the
`felix` database, apply `sql/schema.sql`, and seed `sql/seed_dump.sql` (only when
empty, so re-runs are idempotent):

```bash
docker compose up -d                 # start node + auto create/seed felix
docker compose logs -f crdb-init     # watch the schema/seed step finish
```

- SQL: `localhost:26257`   •   Web console: <http://localhost:8080> (no login, local only)
- Data persists in the `crdb-data` volume across `docker compose down`.
- `docker compose down -v` wipes the volume for a fully fresh start.
- Bump the image tag in `docker-compose.yml` if you want a different CockroachDB
  version (keep it ≥ v24.3 for `VECTOR`).

Because the Docker init step already seeds the DB, you can **skip step 4
(seeding)** on this path.

### Option B — Native binary (Homebrew)

```bash
brew install cockroachdb/tap/cockroach     # installs v26.2.5+

cockroach start-single-node --insecure --store=./.crdb-data \
  --listen-addr=localhost:26257 --http-addr=localhost:8080 --background

cockroach sql --insecure --host=localhost:26257 \
  -e "CREATE DATABASE IF NOT EXISTS felix;"
```

`.crdb-data/` is gitignored (it holds the raw store, ~1GB+). With this path you
seed the DB yourself in step 4.

---

## 2. Python environment

```bash
python3.12 -m venv .venv                       # or your 3.10+ interpreter
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt    # fastapi, uvicorn, psycopg,
                                               # sentence-transformers, google-
                                               # generativeai, boto3, mcp, ...
```

> **Heads-up:** the local embedder (`EMBED_PROVIDER=local`) uses
> `BAAI/bge-large-en-v1.5` (1024-dim). Its weights (~1.3GB) download **on the
> first embedding call at runtime** — not during `pip install`. The first
> `/recall` or `respond` will therefore be slow; subsequent calls are fast.

---

## 3. Configure `.env`

```bash
cp .env.example .env
```

Then set these for local dev (this is the verified local config):

```dotenv
DATABASE_URL=postgresql://root@localhost:26257/felix?sslmode=disable
EMBED_PROVIDER=local
LLM_PROVIDER=gemini
GEMINI_API_KEY=<your key from aistudio.google.com/apikey>
GEMINI_MODEL_ID=gemini-flash-latest
```

`.env` is **gitignored — never commit it** (it holds the real API key). Config
is read once by `src/config.py`; you switch local↔Cloud or model providers by
editing `.env`, never code.

---

## 4. Seed the database

**Skip this if you used the Docker path (Option A) — it already seeded.**

For the native path (Option B), either restore the committed dump (fast, no
re-embedding) …

```bash
cockroach sql --insecure --host=localhost:26257 --database=felix -f sql/schema.sql
cockroach sql --insecure --host=localhost:26257 --database=felix -f sql/seed_dump.sql
```

… **or** seed from scratch (parses the sample project, embeds ~40 rows — this
triggers the bge-large download):

```bash
./.venv/bin/python -m src seed --apply-schema --truncate
```

Sanity-check the row counts (should total 154):

```bash
cockroach sql --insecure --host=localhost:26257 --database=felix \
  -e "SELECT 'incidents' t, count(*) FROM incidents
      UNION ALL SELECT 'doc_chunks', count(*) FROM doc_chunks
      UNION ALL SELECT 'code_changes', count(*) FROM code_changes
      UNION ALL SELECT 'code_nodes', count(*) FROM code_nodes
      UNION ALL SELECT 'code_edges', count(*) FROM code_edges
      UNION ALL SELECT 'resolution_steps', count(*) FROM resolution_steps;"
# incidents 14, doc_chunks 15, code_changes 11, code_nodes 42, code_edges 22, resolution_steps 50
```

---

## 5. Run the backend

### CLI (quick check, no server)

```bash
# retrieval only — no LLM, no DB writes:
./.venv/bin/python -m src respond \
  "checkout failing, db.pool.exhausted during spike" \
  --origin-node ConnectionPool.acquire --no-llm

# full loop (recall → Gemini → diagnosis + write-back):
./.venv/bin/python -m src respond \
  "checkout failing, db.pool.exhausted during spike" \
  --origin-node ConnectionPool.acquire
```

### HTTP API (what the frontend talks to)

```bash
./.venv/bin/python -m src serve --host 127.0.0.1 --port 8000   # add --reload for dev
```

Verify:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}

# retrieval only:
curl -s -X POST http://127.0.0.1:8000/recall \
  -H 'Content-Type: application/json' \
  -d '{"alert":"checkout failing, db.pool.exhausted during traffic spike","origin_node":"ConnectionPool.acquire","k":3}'

# full loop (needs GEMINI_API_KEY):
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"alert":"checkout failing, db.pool.exhausted during traffic spike","origin_node":"ConnectionPool.acquire","k":3}'

# streaming full loop — Server-Sent Events (evidence → deltas → done). -N
# disables curl buffering so you see frames arrive live:
curl -sN -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"alert":"checkout failing, db.pool.exhausted during traffic spike","origin_node":"ConnectionPool.acquire","k":3}'
```

`/chat` returns **503** if `LLM_PROVIDER=gemini` but `GEMINI_API_KEY` is unset —
that's expected; set the key or use `/recall`.

---

## 6. Run the frontend

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

The dev server reads `frontend/.env.local` (committed):
`VITE_API_URL=http://localhost:8000` points it at the backend. The API enables
permissive CORS, so the cross-origin call works directly. With `VITE_API_URL`
unset the UI runs in **mock mode** (no backend needed) — handy for pure UI work.

Build / typecheck:

```bash
npm run build          # tsc --noEmit && vite build
```

---

## 7. Tests

```bash
./.venv/bin/python -m pytest            # deterministic: parsing + write-back (no key/network)
./.venv/bin/python -m pytest -m live    # + planted-puzzle quality checks (real Gemini + DB)
```

The default run needs no API key or network. The `-m live` run needs a seeded DB
and a working `GEMINI_API_KEY`.

---

## Troubleshooting

- **`VECTOR` type errors on schema apply** → your CockroachDB is too old. Use
  ≥ v24.3 (v26.2.5 verified). On the Docker path, bump the image tag.
- **First `/recall` hangs for a minute** → that's the one-time bge-large model
  download (~1.3GB). Subsequent calls are fast.
- **`/chat` → 503** → `GEMINI_API_KEY` isn't set in `.env` (or `LLM_PROVIDER`
  isn't `gemini`).
- **Port 26257 already in use** → you have both a native node *and* the Docker
  node running. Stop one (`docker compose down`, or kill the native
  `cockroach` process). They both bind 26257.
- **Frontend shows mock data** → `VITE_API_URL` is unset; check
  `frontend/.env.local` and restart `npm run dev`.
- **`serve: uvicorn is not installed`** → `pip install -r requirements.txt` into
  the active venv (it includes `fastapi` + `uvicorn[standard]`).
