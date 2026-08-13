# felix — `src/` code overview

A walkthrough of the code under `src/`, layer by layer. For project-level context
(what felix is, the four memory sources, the two planted puzzles, how to run it),
see `CLAUDE.md`.

## Big picture

`src/` is a **strictly layered** codebase. Dependencies point one direction only:

```
cli.py   ──▶  service/  ──▶  store/repositories/  ──▶  store/connection.py
api/     ──▶     │                   │                      │
  │              └──▶ clients/       └──▶ models.py ◀────────┘
  └──▶ seed/          (embedder, llm, mcp)         config.py (read by all)
```

The core job: for an incoming alert, **recall** relevant memory from CockroachDB
(four sources), optionally **trace the code graph**, then **reason** over it with
an LLM and **write the diagnosis back**. Two halves — *retrieval* (deterministic)
and *reasoning* (the LLM) — are kept cleanly separated. `cli.py` and `api/` are
two **thin drivers** over the same service layer; no business logic lives in
either.

## Layer by layer

### `config.py` — the one place env is read
A frozen `Settings` dataclass loaded once from `.env`. Everything swappable (DB
URL, embedder provider, LLM provider, AWS/Bedrock, MCP creds) is an env var, so
you switch local↔Cloud or local-model↔Bedrock by swapping `.env`, never code.
`get_settings()` is an `lru_cache`'d singleton.

### `models.py` — the shared vocabulary
Plain dataclasses that everything speaks in, so raw DB tuples never leak past the
repository layer. Key ones:

- Record types per memory source: `Incident` (+ `ResolutionStep`), `DocChunk`,
  `CodeChange`, `CodeNode`/`CodeEdge`.
- `Recall[T]` — a record + its **L2 vector distance** (lower = closer).
- `GraphHit` — a `CodeNode` + the shallowest **hop depth** it was reached at.
- `EvidencePacket` — everything retrieval gathers for one alert (the input to
  reasoning).
- `Diagnosis` — the reasoning layer's output; `DiagnosisResult` bundles a
  `Diagnosis` with the `EvidencePacket` it was reasoned over plus the
  `session_id` of the conversation it belongs to (so one gather serves both the
  diagnosis and the evidence display, and callers can continue the thread).
  `LLMResult` wraps one LLM completion.
- Working memory: `ActiveIncident` (a live conversation, linked to its episodic
  `incident_id`) + `ActiveIncidentTurn` (one `user`/`agent` message).

### `store/` — persistence
- **`connection.py`**: `get_conn()` (autocommit psycopg3 connection) and the
  important `vec_literal()` helper. CockroachDB's `VECTOR` type has no native
  psycopg adapter, so a Python float list is formatted as a `"[0.1,0.2,…]"`
  string, bound as a normal text param, and cast with `%s::VECTOR(1024)` in SQL —
  no injection risk, no manual interpolation.
- **`repositories/`**: one class per memory source, all extending
  `BaseRepository` (just holds the connection). Each owns the SQL for its
  table(s) and maps rows↔models:
  - `incidents.py` — `insert`, `insert_minimal` (no embedding → invisible to
    recall; used for live write-back), `add_resolution_steps`, and `recall`
    (vector k-NN via `embedding <-> %s::VECTOR(1024)`).
  - `docs.py`, `changes.py` — same recall pattern; **`changes.recall` adds a time
    filter** (`merged_at > now() - N days`) — semantic *and* temporal.
  - `graph.py` — the graph engine. `WITH RECURSIVE` traversal over `code_edges`,
    one shared `_traverse` with the join direction flipped: `blast_radius` walks
    **downstream** (impact of a cause), `upstream_callers` walks **upstream**
    (find the cause from where a symptom surfaced). `find_node_by_name` does
    best-effort fuzzy name resolution (strips prefixes, drops dotted segments,
    suffix-matches). This is *traversed*, never vector-searched.
  - `actions.py` — append-only `agent_actions` audit log; JSONB in/out.
  - `active.py` — working memory (`ActiveIncidentRepository`): `create_session`,
    `get_session` (with transcript), `append_turn` (auto-orders), `set_status`.
    Backs the multi-turn loop.

### `clients/` — swappable external SDKs (all lazy-imported)
- **`embedder/`**: `Embedder` ABC + `get_embedder()`. `titan.py` (Bedrock Titan)
  and `local.py` (bge-large). Both produce exactly **1024 dims** to match the
  schema.
- **`llm/`**: `LLMClient` ABC + `get_llm()`. `gemini.py` (default) and
  `bedrock.py` (Claude). Note `gemini.py`'s careful handling of `response.text`
  raising on SAFETY/MAX_TOKENS.
- **`cockroach_mcp.py`**: thin async client for the CockroachDB Managed MCP
  Server — a stretch/spike, gated on live creds.

Every client imports its heavy SDK *inside* a method, never at module import — so
importing the package is always safe without AWS/Gemini/MCP configured.

### `service/` — the orchestration
- **`evidence_gatherer.py`**: `EvidenceGatherer.gather()` — the *retrieval half*.
  Embeds the alert once, then fans out to all four repositories, assembling an
  `EvidencePacket`. Fully deterministic; stops before the LLM.
- **`diagnoser.py`**: `IncidentDiagnoser` — the *reasoning half*. Its
  `respond(alert, session_id=None)` runs the loop and returns a `DiagnosisResult`
  (diagnosis + packet + session); `diagnose()` is a back-compat wrapper returning
  just the `Diagnosis`. The loop:
  0. **continue?** if `session_id` names a known conversation (and an
     `active_repo` is wired), load its transcript — this turn is a follow-up
  1. recall (via the gatherer — runs every turn so the evidence panel stays live)
  2. **origin-node resolution** (if caller didn't pin one, mine
     code-symbol-shaped tokens from the top *close-match* incident/doc and
     resolve to a real node, then run the upstream trace)
  3. build a structured prompt (folding in the prior transcript on follow-ups)
  4. call the LLM
  5. **defensive parse** (`_extract_json_object` tolerates fences/prose, never
     raises) with a **citation-integrity guard** (drops any cited id not verbatim
     in the packet)
  6. **atomic write-back**, split by turn kind: first turn mints an episodic
     incident (+ steps + audit) and opens a session; a follow-up records the
     exchange in working memory ONLY (no new incident) — each in one transaction
  7. return the `DiagnosisResult` (with the `session_id` to continue)

  `active_repo` is optional: unset (the CLI, the write-back tests) → single-turn,
  `session_id` stays `None`, behaviour unchanged. The API and frontend thread it.

### `api/` — HTTP driver (FastAPI)
A thin adapter over the service layer, parallel to the CLI — no business logic.
- **`app.py`**: `create_app()` builds the FastAPI app with permissive CORS and a
  per-request connection dependency (`db_conn`). Routes are declared `def` (not
  `async def`) so FastAPI runs the blocking psycopg/LLM calls in a threadpool.
  Endpoints: `POST /chat` → `{diagnosis, evidence, session_id}` (full loop via
  `IncidentDiagnoser.respond`; accepts an optional `session_id` in the body to
  continue a conversation as a follow-up), `POST /recall` → `{evidence}`
  (retrieval only, the `--no-llm` equivalent), `GET /health`. `/chat` returns 503
  if the LLM isn't configured.
- **`schemas.py`**: serializes domain models to the JSON contract in
  `frontend/src/api/types.ts` (recalls → `{item, distance}`, graph hits →
  `{node, depth}`, datetimes → ISO strings).

Run it with `python -m src serve [--reload]` (see `cli.py`).

### `cli.py` / `__main__.py` — entry point
`python -m src {respond,seed,parse,mcp-probe,serve}`. `respond` prints the
evidence packet as blocks [1]–[4], then the diagnosis as [5] (`--no-llm` stops at
[4], no DB writes); the LLM path calls `respond()` so a single gather serves both
blocks. `serve` launches the HTTP API. Wires the connection, gatherer,
repositories, and diagnoser together.

### `seed/` — populating memory
- **`parser.py`**: pure-stdlib AST walker turning the
  `sample_project/checkout_service` into a code graph (~42 nodes / 22 edges).
  Deterministic `uuid5` ids; multi-pass best-effort call resolution (return
  types, `self.attr` types, import + call edges). No DB, no network.
- **`loader.py`**: the `Seeder` — the integration seam. Parses the graph, loads
  the three JSON corpora, embeds each row's searchable text, inserts via the
  repositories. Seed string ids (`inc-0001`) become stable UUIDs via `uuid5`;
  `--truncate` reseeds cleanly.

## Things worth flagging

- **Two id worlds don't fully connect.** Recall exposes UUIDs
  (`uuid5("inc-0001")`), and nothing in the retrieval layer maps them back to the
  human `inc-0001`. `diagnoser.py` documents this at length (the
  `_build_prompt` citation-id note): it cites the UUID because that's what's
  verbatim in the packet. A human-id passthrough is the noted follow-up.
- **The frontend and API share one contract.** `frontend/src/api/types.ts`
  mirrors `models.py`; `api/schemas.py` produces exactly that shape. Change one,
  change all three.
</content>
