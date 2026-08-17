/**
 * API contract between the felix frontend and the (to-be-built) HTTP backend.
 *
 * These types mirror the domain models in `src/models.py`. When you wire up the
 * backend (a thin FastAPI/Flask wrapper around IncidentResponder.diagnose), have
 * it serialize responses to exactly this shape and the frontend needs no changes.
 *
 * Endpoint (see src/api/client.ts):
 *   POST  ${VITE_API_URL}/chat
 *   body  ChatRequest
 *   200   ChatResponse
 */

// ── memory-source records (mirror src/models.py) ────────────────────────────

export interface ResolutionStep {
  step_order: number;
  action: string;
  command: string | null;
  outcome: string | null;
}

export interface Incident {
  id: string;
  title: string;
  symptoms: string;
  root_cause: string | null;
  service: string | null;
  severity: string | null;
  tags?: string[];
  occurred_at?: string | null;
  resolution_steps?: ResolutionStep[];
  /**
   * Human feedback on a live-diagnosed incident: "helpful" (confirmed → embedded
   * into recallable memory), "not_helpful" (kept out of recall), or null/absent
   * (unreviewed, or a seeded incident). See POST /incidents/{id}/feedback.
   */
  feedback?: "helpful" | "not_helpful" | null;
}

export interface DocChunk {
  id: string;
  doc_title: string;
  heading: string | null;
  body: string;
  doc_type: string | null;
  source_path?: string | null;
}

export interface CodeChange {
  id: string;
  commit_sha: string;
  merged_at: string; // ISO 8601
  title: string;
  summary: string | null;
  author?: string | null;
  files_changed?: string[];
  services_affected?: string[];
  affected_components?: string[];
}

export interface CodeNode {
  id: string;
  name: string;
  kind: string;
  file: string | null;
  service: string | null;
  source: string | null;
  summary: string | null;
  last_commit?: string | null;
}

// ── recall wrappers ─────────────────────────────────────────────────────────

/** A recalled record plus its L2 distance from the query vector (lower = closer). */
export interface Recall<T> {
  item: T;
  distance: number;
}

/** A code node reached during graph traversal, with its shallowest hop depth. */
export interface GraphHit {
  node: CodeNode;
  depth: number;
}

// ── assembled evidence + diagnosis ──────────────────────────────────────────

/** Everything the retriever gathered for one alert (blocks [1]-[4] in the CLI). */
export interface EvidencePacket {
  alert: string;
  incidents: Recall<Incident>[];
  docs: Recall<DocChunk>[];
  changes: Recall<CodeChange>[];
  upstream: GraphHit[];
}

/**
 * The reasoning layer's output (block [5]). `proposed_steps` may arrive as
 * plain strings or as structured step objects — the UI renders both.
 */
export interface Diagnosis {
  summary: string;
  root_cause: string | null;
  proposed_steps: Array<ProposedStep | string>;
  cited_incident_ids: string[];
  cited_change_ids: string[];
  confidence: number | null;
  incident_id: string | null;
}

export interface ProposedStep {
  action: string;
  command?: string | null;
  outcome?: string | null;
}

/**
 * The *other* response shape besides a full `Diagnosis` — a lightweight,
 * conversational reply for follow-ups where a rigid root-cause/steps card would
 * be overkill (e.g. "how do I rerun the service?"). `text` is GitHub-flavored
 * Markdown; it can still cite recalled memory. The model chooses `Diagnosis` vs
 * `Message` per turn (see `ChatResponse.response_type`).
 */
export interface Message {
  text: string; // GitHub-flavored Markdown
  cited_incident_ids: string[];
  cited_change_ids: string[];
  incident_id: string | null;
}

// ── request / response envelope ─────────────────────────────────────────────

export interface ChatRequest {
  alert: string;
  /** Optional: code_nodes.name where the symptom surfaces (enables the graph trace). */
  origin_node?: string | null;
  /** Optional: results per source (defaults to backend's choice, e.g. 3). */
  k?: number;
  /**
   * Optional: an active-incident conversation id from a prior ChatResponse.
   * Set it to ask a follow-up in the same conversation (multi-turn); omit it
   * to open a fresh incident.
   */
  session_id?: string | null;
}

/**
 * The /chat (and /chat/stream `done`) envelope. `response_type` is the
 * discriminator: exactly one of `diagnosis` / `message` is populated (the other
 * is null). A first turn is always a `diagnosis`; a follow-up is usually a
 * lightweight `message`, or a fresh `diagnosis` if the engineer reports a
 * materially new problem.
 */
export interface ChatResponse {
  response_type: "diagnosis" | "message";
  diagnosis: Diagnosis | null;
  message: Message | null;
  evidence: EvidencePacket;
  /** The conversation this turn belongs to; echo it on the next request to continue. */
  session_id?: string | null;
}

// ── incident library (browse + semantic search) ─────────────────────────────

/**
 * One incident in the library view. `distance` is the L2 vector distance from
 * the search query (lower = closer) when the list came from a semantic search,
 * or null when browsing the whole library unranked. Same shape either way so
 * the page renders one list. See `GET /incidents` and `GET /incidents/search`.
 */
export interface IncidentHit {
  item: Incident;
  distance: number | null;
}

export interface IncidentsResponse {
  incidents: IncidentHit[];
  /** Echoed back by the search endpoint; absent when browsing. */
  query?: string;
}

/**
 * Response from POST /incidents/{id}/feedback. `recallable` reflects whether the
 * incident now carries an embedding (true after 👍, false after 👎).
 */
export interface FeedbackResponse {
  incident_id: string;
  feedback: "helpful" | "not_helpful";
  recallable: boolean;
}

// ── real-time CDC alerts (see .orchestration/CDC_INTERFACE.md §7) ────────────

/**
 * A live alert the watcher raised off the metrics changefeed, as served by
 * `GET /alerts`. `summary` is the synthesized alert text verbatim; `service`
 * and `metric` are parsed from the session's origin_node and may be null.
 */
export interface AlertPayload {
  session_id: string;
  service: string | null;
  metric: string | null;
  summary: string;
  created_at: string; // ISO 8601
  status: string;
}

/**
 * One live telemetry sample, as served by `GET /metrics/recent` (backfill) and
 * streamed over `GET /metrics/stream` (Server-Sent Events, the CDC path). A
 * timing probe on an instrumented service writes one row per measured call;
 * both delivery paths serialize to this identical shape. `value` is the metric
 * value (ms for a `*_latency_ms` metric); `labels` is arbitrary JSON (e.g.
 * `{"ok": false}` when the measured call raised).
 */
export interface MetricSample {
  service: string;
  metric: string;
  value: number;
  ts: string; // ISO 8601
  labels?: Record<string, unknown> | null;
}

/**
 * Default alert levels for the live-monitoring panel, from `GET /metrics/config`
 * (env-configured on the backend). `default_p99_ms` applies to any latency
 * metric without a specific entry; `thresholds` maps a metric name to its own
 * p99 threshold. The panel seeds each card's alert level from this — the
 * operator can still override any card's level live.
 */
export interface MetricConfig {
  default_p99_ms: number;
  thresholds: Record<string, number>;
}

// ── DB overview (via the CockroachDB Cloud MCP server) ───────────────────────

/** Cluster metadata from the MCP `get_cluster` tool. */
export interface DbClusterInfo {
  id: string;
  name: string;
  cockroach_version: string;
  cloud_provider: string;
  state: string;
  plan: string;
  regions: Array<{ name: string; node_count: number }>;
  created_at: string;
  updated_at: string;
}

/** One database row from the MCP `list_databases` tool. */
export interface DbDatabase {
  database_name: string;
  owner?: string | null;
  primary_region?: string | null;
  regions?: string[];
}

/** One table row from the MCP `list_tables` tool. */
export interface DbTable {
  schema_name: string;
  table_name: string;
  type: string;
  owner?: string | null;
  estimated_row_count?: number | null;
  locality?: string | null;
}

/**
 * Response from `GET /db/overview` — a read-only cluster snapshot gathered
 * through the CockroachDB Cloud Managed MCP Server (felix as its own MCP
 * client). `connected` is false (with a `reason`) when the MCP endpoint isn't
 * configured or auth/connection failed, so the panel renders a soft state
 * rather than erroring. `tools_used` names the MCP tools this snapshot invoked.
 */
export interface DbOverview {
  connected: boolean;
  reason?: string;
  source?: string;
  cluster?: DbClusterInfo | null;
  databases?: DbDatabase[];
  tables_by_db?: Record<string, DbTable[]>;
  running_queries?: Array<Record<string, unknown>>;
  tools_used?: string[];
}

/**
 * A reviewable DB-write plan from `POST /db/plan` — felix's natural-language
 * request mapped to exactly ONE CockroachDB MCP tool call, NOT yet executed
 * (preview-then-confirm). `tool` is an entry in the additive-write allowlist
 * (create_table / create_database / insert_rows / select_query); `args` is the
 * single argument that tool takes; `write` is false for a read-only SELECT.
 */
export interface DbPlan {
  tool: string;
  args: Record<string, string>;
  explanation: string;
  write: boolean;
}

/**
 * Response from `POST /db/plan`. `plan` is the mapped tool call to preview, or
 * null with a `reason` when the request couldn't be mapped safely (ambiguous,
 * destructive/unsupported, or not a DB operation).
 */
export interface DbPlanResponse {
  plan: DbPlan | null;
  reason?: string;
}

/**
 * Result of `POST /db/execute` — the plan actually run against the cluster over
 * MCP. `ok` is false (with `error`) on a tool/MCP failure; `result` is the raw
 * tool payload (e.g. affected-row info, or SELECT rows) on success.
 */
export interface DbExecuteResult {
  ok: boolean;
  tool: string;
  args: Record<string, string>;
  result?: unknown;
  error?: unknown;
}

/**
 * Status of the CLI panel's terminal, from `GET /cli/status`. `enabled` gates
 * the whole panel (FELIX_CLI_ENABLED on the backend); `ccloud_installed` /
 * `account` let the panel show whether `ccloud` is present and which Cloud
 * account it's authed as, so the operator isn't staring at a blank shell.
 */
export interface CliStatus {
  enabled: boolean;
  ccloud_installed: boolean;
  ccloud_path: string | null;
  account: string | null;
  cluster_id: string | null;
}

/** One pre-seeded turn of a CDC session's transcript (`GET /sessions/{id}`). */
export interface SessionTurn {
  turn_order: number;
  role: string;
  content: string;
}

/**
 * A triage session's full state from `GET /sessions/{id}`: enough to render the
 * pre-seeded turns and then continue via `/chat/stream`. `diagnosis` is
 * reconstructed from the linked incident; `evidence` is null on this endpoint
 * (live evidence arrives when the operator sends a follow-up).
 */
export interface SessionResponse {
  session_id: string;
  source: string;
  status: string;
  alert: string;
  origin_node: string | null;
  turns: SessionTurn[];
  incident_id: string | null;
  diagnosis: Diagnosis | null;
  evidence: EvidencePacket | null;
}

/**
 * Callbacks for the streaming path (POST /chat/stream, Server-Sent Events).
 * `onEvidence` fires once as soon as recall completes (so the evidence panel
 * fills while the model is still reasoning); `onDelta` fires for each chunk of
 * the model's output; exactly one of `onDone` / `onError` terminates the stream.
 */
export interface StreamHandlers {
  onEvidence?: (evidence: EvidencePacket) => void;
  onDelta?: (text: string) => void;
  onDone: (response: ChatResponse) => void;
  onError: (message: string) => void;
}
