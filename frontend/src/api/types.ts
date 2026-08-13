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

export interface ChatResponse {
  diagnosis: Diagnosis;
  evidence: EvidencePacket;
  /** The conversation this turn belongs to; echo it on the next request to continue. */
  session_id?: string | null;
}
