# felix — `src/` code overview

A walkthrough of the code under `src/`, layer by layer. For project-level context
(what felix is, the four memory sources, the two planted puzzles, how to run it),
see `CLAUDE.md`.

## Big picture

`src/` is a **strictly layered** codebase. Dependencies point one direction only:

```
cli.py  ──▶  service/  ──▶  store/repositories/  ──▶  store/connection.py
  │             │                   │                      │
  │             └──▶ clients/       └──▶ models.py ◀────────┘
  └──▶ seed/          (embedder, llm, mcp)         config.py (read by all)
```

The core job: for an incoming alert, **recall** relevant memory from CockroachDB
(four sources), optionally **trace the code graph**, then **reason** over it with
an LLM and **write the diagnosis back**. Two halves — *retrieval* (deterministic)
and *reasoning* (the LLM) — are kept cleanly separated.

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
- `Diagnosis` / `LLMResult` — the reasoning layer's output.

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
- **`retriever.py`**: `Retriever.gather()` — the *retrieval half*. Embeds the
  alert once, then fans out to all four repositories, assembling an
  `EvidencePacket`. Fully deterministic; stops before the LLM.
- **`responder.py`**: `IncidentResponder.diagnose()` — the *reasoning half*, a
  7-step loop:
  1. recall (via retriever)
  2. **origin-node resolution** (if caller didn't pin one, mine
     code-symbol-shaped tokens from the top *close-match* incident/doc and
     resolve to a real node, then run the upstream trace)
  3. build a structured prompt
  4. call the LLM
  5. **defensive parse** (`_extract_json_object` tolerates fences/prose, never
     raises) with a **citation-integrity guard** (drops any cited id not verbatim
     in the packet)
  6. **atomic write-back** (minimal incident + resolution_steps + audit row, all
     in one transaction)
  7. return

### `cli.py` / `__main__.py` — entry point
`python -m src {respond,seed,parse,mcp-probe}`. `respond` prints the evidence
packet as blocks [1]–[4], then the diagnosis as [5] (`--no-llm` stops at [4], no
DB writes). Wires the connection, retriever, repositories, and responder
together.

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
  human `inc-0001`. `responder.py` documents this at length (lines 189–203): it
  cites the UUID because that's what's verbatim in the packet. A human-id
  passthrough is the noted follow-up.
- **`respond` block [4]'s footer text is stale** — `_print_packet` still prints
  "NEXT (not yet built): hand this packet to the reasoning model…" (cli.py:60),
  but the reasoning step *is* built now and prints right after as [5]. Minor
  cosmetic mismatch.
</content>
</invoke>
