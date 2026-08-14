/**
 * Mock backend — canned ChatResponses so the UI runs with no server.
 *
 * The data mirrors felix's two planted puzzles (see WORLD.md / CLAUDE.md):
 *   A. "code-only"  — db.pool.exhausted; cause is in the call graph.
 *   B. "merge-only" — slow checkout, green dashboards; cause is a recent merge.
 * A crude keyword match picks the scenario; anything else gets a generic reply.
 *
 * Delete this file (and its use in client.ts) once the real backend is wired.
 */

import type { ChatRequest, ChatResponse, StreamHandlers } from "./types";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const POOL_SCENARIO: ChatResponse = {
  response_type: "diagnosis",
  message: null,
  diagnosis: {
    summary:
      "Connection-pool exhaustion during traffic spikes is driven by checkout holding a pooled connection across the payment gateway's retry loop — not a DB capacity problem.",
    root_cause:
      "CheckoutHandler.process acquires a ConnectionPool connection and keeps it held while awaiting PaymentClient.charge, whose retry/backoff loop can run for seconds. Under load, connections drain faster than they return, so ConnectionPool.acquire starts raising db.pool.exhausted. Scaling the DB does not help because the pool, not the DB, is the bottleneck.",
    proposed_steps: [
      {
        action:
          "Release the pooled connection before calling PaymentClient.charge; re-acquire only for the post-charge write.",
        command: null,
        outcome:
          "Connections are no longer held across slow gateway retries; pool utilization drops under the same load.",
      },
      {
        action:
          "Add a bounded timeout / max-retry budget to the payment charge path so a slow gateway can't pin a connection indefinitely.",
        command: null,
        outcome: "Worst-case hold time per request becomes bounded and predictable.",
      },
      {
        action: "Add a metric for pool checkout duration and alert on p99.",
        command: null,
        outcome: "Regressions in hold time surface before they exhaust the pool.",
      },
    ],
    cited_incident_ids: ["mock-inc-a"],
    cited_change_ids: [],
    confidence: 0.82,
    incident_id: "mock-new-incident-a",
  },
  evidence: {
    alert: "checkout failing, db.pool.exhausted during spike",
    incidents: [
      {
        distance: 0.41,
        item: {
          id: "mock-inc-a",
          title: "Checkout latency during flash sale",
          symptoms:
            "Elevated checkout latency and intermittent errors during a traffic spike; connection pool saturated.",
          root_cause:
            "Investigated as DB capacity at the time; scaling the primary gave only partial relief.",
          service: "checkout-service",
          severity: "SEV2",
        },
      },
    ],
    docs: [
      {
        distance: 0.52,
        item: {
          id: "mock-doc-1",
          doc_title: "Checkout service runbook",
          heading: "Connection pool",
          body: "The checkout service uses a fixed-size connection pool (connection-pool). db.pool.exhausted indicates all connections are checked out. Historically treated as a DB-scaling signal.",
          doc_type: "runbook",
        },
      },
    ],
    changes: [],
    upstream: [
      {
        depth: 0,
        node: {
          id: "n-acquire",
          name: "ConnectionPool.acquire",
          kind: "function",
          file: "sample_project/checkout_service/pool.py",
          service: "checkout-service",
          summary: "Check out a connection from the pool; raise db.pool.exhausted if none free.",
          source: null,
        },
      },
      {
        depth: 2,
        node: {
          id: "n-charge",
          name: "PaymentClient.charge",
          kind: "function",
          file: "sample_project/checkout_service/payment.py",
          service: "checkout-service",
          summary: "Charge the payment gateway, with retry + backoff on transient failures.",
          source: null,
        },
      },
      {
        depth: 3,
        node: {
          id: "n-process",
          name: "CheckoutHandler.process",
          kind: "function",
          file: "sample_project/checkout_service/handler.py",
          service: "checkout-service",
          summary: "Orchestrate a checkout: acquire a connection, charge, then persist the order.",
          source:
            "def process(self, order):\n    conn = self._pool.acquire()          # <-- held across the slow call below\n    try:\n        self._payment.charge(order)      # retry loop can run for seconds\n        self._orders.save(order, conn)\n    finally:\n        self._pool.release(conn)",
        },
      },
    ],
  },
};

const LATENCY_SCENARIO: ChatResponse = {
  response_type: "diagnosis",
  message: null,
  diagnosis: {
    summary:
      "Customers see slow checkout while dashboards stay green because tail latency is being hidden by a recent metrics change, not because latency improved.",
    root_cause:
      "A recent merge changed LATENCY_AGGREGATION in metrics.py from \"p99\" to \"avg\". The dashboards now plot mean latency, which stays flat even as p99 climbs — so no alert fires despite real user-facing slowness.",
    proposed_steps: [
      {
        action: "Revert LATENCY_AGGREGATION back to \"p99\" in metrics.py.",
        command: "git revert <sha of chg-0001>",
        outcome: "Dashboards resume tracking tail latency; the existing SLO alert can fire again.",
      },
      {
        action: "Backfill/recompute the affected dashboards and confirm p99 reflects the reported slowness.",
        command: null,
        outcome: "The regression becomes visible for the window it was hidden.",
      },
    ],
    cited_incident_ids: [],
    cited_change_ids: ["mock-chg-0001"],
    confidence: 0.78,
    incident_id: "mock-new-incident-b",
  },
  evidence: {
    alert: "customers report slow checkout but dashboards look fine",
    incidents: [],
    docs: [
      {
        distance: 0.55,
        item: {
          id: "mock-doc-2",
          doc_title: "Observability standards",
          heading: "Latency SLOs",
          body: "Checkout latency SLOs are defined on p99, not average. Alerts fire when p99 breaches the threshold.",
          doc_type: "standard",
        },
      },
    ],
    changes: [
      {
        distance: 0.38,
        item: {
          id: "mock-chg-0001",
          commit_sha: "a1b2c3d",
          merged_at: "2026-08-05T14:22:00Z",
          title: "metrics: switch latency aggregation to avg for smoother graphs",
          summary:
            "Changed LATENCY_AGGREGATION from \"p99\" to \"avg\" in metrics.py to reduce dashboard noise.",
          author: "someone",
          files_changed: ["metrics.py"],
          services_affected: ["checkout-service"],
          affected_components: ["metrics"],
        },
      },
    ],
    upstream: [],
  },
};

function genericScenario(req: ChatRequest): ChatResponse {
  return {
    response_type: "diagnosis",
    message: null,
    diagnosis: {
      summary: `(mock) No canned scenario matched this alert. Wire up the real backend to get a live diagnosis.`,
      root_cause: null,
      proposed_steps: [
        "This is mock mode — set VITE_API_URL to your felix backend to get real recall + reasoning.",
      ],
      cited_incident_ids: [],
      cited_change_ids: [],
      confidence: null,
      incident_id: null,
    },
    evidence: {
      alert: req.alert,
      incidents: [],
      docs: [],
      changes: [],
      upstream: [],
    },
  };
}

/**
 * A canned follow-up reply, so mock mode also demonstrates multi-turn AND the
 * lightweight `message` shape: once a conversation is open (req.session_id set)
 * we answer the follow-up conversationally (Markdown) rather than re-diagnosing
 * with a full root-cause/steps card. Evidence is left empty — the panel keeps
 * showing the original turn's evidence.
 */
function followUpScenario(req: ChatRequest): ChatResponse {
  return {
    response_type: "message",
    diagnosis: null,
    message: {
      text:
        `Scaling the DB alone won't clear this — **the pool, not the DB, is the bottleneck**.\n\n` +
        `1. Release the connection *before* the payment retry loop\n` +
        `2. Re-check pool checkout p99:\n\n` +
        "```bash\n" +
        "kubectl exec deploy/checkout-service -- \\\n" +
        "  curl -s localhost:9090/metrics | grep pool_checkout_seconds\n" +
        "```",
      cited_incident_ids: [],
      cited_change_ids: [],
      incident_id: null,
    },
    session_id: req.session_id,
    evidence: { alert: req.alert, incidents: [], docs: [], changes: [], upstream: [] },
  };
}

// Deterministic mock session ids (no Date.now/random needed for a demo).
let mockSession = 0;
function newMockSessionId(): string {
  mockSession += 1;
  return `mock-session-${mockSession}`;
}

/** Pick a canned scenario from alert keywords (no artificial latency). */
function resolveScenario(req: ChatRequest): ChatResponse {
  // Follow-up within an open conversation.
  if (req.session_id) {
    return followUpScenario(req);
  }

  const session_id = newMockSessionId();
  const a = req.alert.toLowerCase();
  if (a.includes("pool") || a.includes("exhaust") || a.includes("connection")) {
    return { ...POOL_SCENARIO, session_id, evidence: { ...POOL_SCENARIO.evidence, alert: req.alert } };
  }
  if (a.includes("slow") || a.includes("latency") || a.includes("p99") || a.includes("dashboard")) {
    return { ...LATENCY_SCENARIO, session_id, evidence: { ...LATENCY_SCENARIO.evidence, alert: req.alert } };
  }
  return { ...genericScenario(req), session_id };
}

/** Pick a canned scenario from alert keywords. Simulates network latency. */
export async function mockChat(req: ChatRequest): Promise<ChatResponse> {
  await sleep(550);
  return resolveScenario(req);
}

/**
 * Streaming mock: mirrors the real `/chat/stream` sequence — evidence first,
 * then the summary dribbled out word-by-word as deltas, then done — so the
 * live-reasoning UI can be developed with no backend. (The real backend streams
 * the model's raw JSON; mock streams the prose summary for a nicer preview.)
 */
export async function mockChatStream(req: ChatRequest, handlers: StreamHandlers): Promise<void> {
  const response = resolveScenario(req);
  await sleep(300);
  handlers.onEvidence?.(response.evidence);
  // Stream whichever shape this turn produced: the message body for a
  // conversational follow-up, else the diagnosis summary.
  const preview =
    response.response_type === "message"
      ? (response.message?.text ?? "")
      : (response.diagnosis?.summary ?? "");
  for (const word of preview.split(/(\s+)/)) {
    await sleep(28);
    handlers.onDelta?.(word);
  }
  await sleep(150);
  handlers.onDone(response);
}
