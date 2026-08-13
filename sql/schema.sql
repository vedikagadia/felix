-- Felix memory schema — four memory sources, one CockroachDB database.
-- Apply with: cockroach sql --url "$DATABASE_URL" -f sql/schema.sql
--          or psql "$DATABASE_URL" -f sql/schema.sql
--
-- Memory sources (see docs/ARCHITECTURE.md):
--   incidents     — episodic memory: past symptom→cause→fix records (semantic recall)   [~50%]
--   doc_chunks    — project documentation, chunked by heading (semantic recall)          [~40%]
--   code_nodes/   — current-state structural mirror of the codebase (graph traversal)    [~10%]
--     code_edges
--   code_changes  — append-only log of merges to main (semantic + temporal recall)
--
-- Recall = run the vector query against incidents + doc_chunks (+ time-filtered code_changes),
-- each returns top-k with distance, merge in the app by distance. The code graph is *traversed*
-- (WITH RECURSIVE), not vector-searched, in this phase.
--
-- NOTE VECTOR(1024): dimension matches Titan Text Embeddings V2. If the vector index errors on
-- the Basic (free) tier, comment out the CREATE VECTOR INDEX lines — recall falls back to an
-- exact nearest-neighbor scan (ORDER BY embedding <-> $1 LIMIT k), instant at seed scale.

-- ── Episodic memory: past incidents ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       STRING NOT NULL,
    symptoms    STRING NOT NULL,           -- the searchable "what's happening"
    root_cause  STRING,
    service     STRING,
    severity    STRING,
    tags        STRING[],
    occurred_at TIMESTAMPTZ,               -- when the incident happened (distinct from created_at, when recorded)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding   VECTOR(1024)               -- Titan V2 of (title + symptoms)
);
CREATE VECTOR INDEX IF NOT EXISTS incidents_embedding_idx ON incidents (embedding);

-- Ordered list of steps that actually resolved an incident — the reusable "playbook"
-- the agent recalls and presents. This is CURATED KNOWLEDGE, distinct from agent_actions
-- (live telemetry of what the agent did). Separate table so steps are queryable across
-- incidents (e.g. "every incident fixed by restarting payment-worker").
CREATE TABLE IF NOT EXISTS resolution_steps (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    step_order   INT NOT NULL,             -- 1, 2, 3 … order the steps were applied
    action       STRING NOT NULL,          -- plain-English what was done
    command      STRING,                   -- the exact command, if any
    outcome      STRING,                   -- what it achieved
    UNIQUE (incident_id, step_order)
);
CREATE INDEX IF NOT EXISTS resolution_steps_incident_idx ON resolution_steps (incident_id, step_order);

-- ── Project documentation, chunked by heading ───────────────────────────────
CREATE TABLE IF NOT EXISTS doc_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_title   STRING NOT NULL,           -- e.g. "Setup Guide", "Architecture"
    heading     STRING,                    -- the section — enables precise citation
    body        STRING NOT NULL,           -- chunk text (embedded with heading)
    doc_type    STRING,                    -- how_it_works | setup | runbook
    source_path STRING,                    -- notional source doc path, for citation
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding   VECTOR(1024)               -- Titan V2 of (heading + body)
);
CREATE VECTOR INDEX IF NOT EXISTS doc_chunks_embedding_idx ON doc_chunks (embedding);

-- ── Structural memory: current-state code graph ─────────────────────────────
-- Mirrors the latest main. Node ids are deterministic (uuid5 of
-- service:file:kind:qualified_name) so re-syncs UPSERT instead of duplicating.
CREATE TABLE IF NOT EXISTS code_nodes (
    id          UUID PRIMARY KEY,          -- deterministic (uuid5), set by the sync script
    name        STRING NOT NULL,
    kind        STRING NOT NULL,           -- class | module | service | function
    file        STRING,
    service     STRING,
    source      STRING,                    -- the actual code of this symbol (fed to the model)
    summary     STRING,                    -- optional one-line description
    last_commit STRING,                    -- commit this reflects → "as of <sha>"
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS code_edges (
    src_id  UUID NOT NULL REFERENCES code_nodes(id) ON DELETE CASCADE,
    dst_id  UUID NOT NULL REFERENCES code_nodes(id) ON DELETE CASCADE,
    kind    STRING NOT NULL,               -- calls | imports | depends_on
    PRIMARY KEY (src_id, dst_id, kind)
);

-- Blast radius from a failing component:
--   WITH RECURSIVE reach(id, depth) AS (
--     SELECT id, 0 FROM code_nodes WHERE name = $failing
--     UNION ALL
--     SELECT e.dst_id, r.depth+1 FROM code_edges e JOIN reach r ON e.src_id = r.id
--       WHERE r.depth < $k
--   ) SELECT DISTINCT n.* FROM reach r JOIN code_nodes n ON n.id = r.id;

-- ── Recent changes: append-only log of merges to main ────────────────────────
-- Answers the first question in any incident: "what changed recently?"
-- Recall is semantic AND temporal: rank recent merges by similarity to the alert.
CREATE TABLE IF NOT EXISTS code_changes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    commit_sha          STRING NOT NULL,
    merged_at           TIMESTAMPTZ NOT NULL,   -- for time-window filtering
    author              STRING,
    title               STRING NOT NULL,        -- PR / commit title
    summary             STRING,                 -- LLM-generated: what changed + likely impact
    files_changed       STRING[],
    services_affected   STRING[],
    affected_components STRING[],                -- code_node names touched; resolve to code_nodes.id in a later graph-enrichment pass (nullable)
    embedding           VECTOR(1024)            -- Titan V2 of (title + summary)
);
CREATE INDEX IF NOT EXISTS code_changes_merged_at_idx ON code_changes (merged_at DESC);
CREATE VECTOR INDEX IF NOT EXISTS code_changes_embedding_idx ON code_changes (embedding);

-- ── Audit log (append-only record of everything the agent did) ──────────────
CREATE TABLE IF NOT EXISTS agent_actions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    action_type  STRING NOT NULL,          -- embed | recall | reason | write_memory
    tool_called  STRING,                   -- e.g. mcp.query, bedrock.invoke_model
    input        JSONB,
    output       JSONB,
    model        STRING,
    tokens       INT
);

-- ── Working memory: active (in-flight) incident conversations ───────────────
-- One row per ongoing conversation with felix — the multi-turn agent loop's
-- scratchpad. Distinct from `incidents` (episodic long-term memory): an active
-- incident is the LIVE session, and it links to the episodic `incidents` row
-- written on the first diagnosis so follow-ups ("did scaling the DB help?")
-- reason with the original diagnosis in context. Rows here are transient and
-- can be pruned once `status = 'resolved'`.
CREATE TABLE IF NOT EXISTS active_incidents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- the session id
    alert       STRING NOT NULL,                              -- the opening alert
    origin_node STRING,                                       -- code_nodes.name, if pinned
    incident_id UUID REFERENCES incidents(id) ON DELETE SET NULL,  -- episodic row from turn 1
    status      STRING NOT NULL DEFAULT 'open',               -- open | resolved
    source      STRING NOT NULL DEFAULT 'chat',               -- 'chat' | 'cdc' — how the session was opened
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotent migration for already-seeded DBs: CREATE TABLE IF NOT EXISTS won't
-- add the column to a table that predates it, so ALTER it in explicitly.
ALTER TABLE active_incidents ADD COLUMN IF NOT EXISTS source STRING NOT NULL DEFAULT 'chat';
CREATE INDEX IF NOT EXISTS active_incidents_status_idx ON active_incidents (status, updated_at DESC);

-- Ordered transcript of one active-incident conversation. Child table (not
-- JSONB) so turns stay queryable, mirroring resolution_steps' design.
CREATE TABLE IF NOT EXISTS active_incident_turns (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES active_incidents(id) ON DELETE CASCADE,
    turn_order  INT NOT NULL,                                 -- 1, 2, 3 … conversation order
    role        STRING NOT NULL,                              -- user | agent
    content     STRING NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, turn_order)
);
CREATE INDEX IF NOT EXISTS active_incident_turns_session_idx ON active_incident_turns (session_id, turn_order);

-- ── Live service metrics: the CDC source table ──────────────────────────────
-- The sample checkout service INSERTs one row per emitted metric sample. The
-- felix watcher (python -m src watch) holds a sinkless CHANGEFEED on this table
-- and reacts to anomalies in real time. Not a memory source — it is transient
-- operational telemetry (prune freely); nothing vector-searches it.
CREATE TABLE IF NOT EXISTS metrics (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service  STRING NOT NULL,               -- e.g. "checkout-service"
    metric   STRING NOT NULL,               -- e.g. "checkout_latency_ms", "pool_in_use"
    value    FLOAT NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    labels   JSONB                          -- optional dims, e.g. {"attempt": 3}
);
CREATE INDEX IF NOT EXISTS metrics_service_metric_ts_idx ON metrics (service, metric, ts DESC);
