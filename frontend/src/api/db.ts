/**
 * The DB-overview seam: fetches a read-only cluster snapshot from
 * `GET /db/overview`, which the backend gathers through the **CockroachDB Cloud
 * Managed MCP Server** (felix as its own MCP client — see
 * `src/clients/cockroach_mcp.py`). The panel renders whatever this returns; the
 * backend already degrades to `{connected:false, reason}` on any MCP failure,
 * so this seam just forwards that shape.
 *
 * Mock mode has no backend, so it synthesizes a representative snapshot (the
 * live `felix-db` cluster + its seed tables) with `connected:false` marked via
 * a note, so the panel is demoable offline.
 */

import { usingMock } from "./client";
import type { DbExecuteResult, DbOverview, DbPlan, DbPlanResponse } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;

export async function fetchDbOverview(): Promise<DbOverview> {
  if (usingMock) return mockDbOverview();

  const res = await fetch(`${API_URL}/db/overview`);
  if (!res.ok) throw new Error(`Backend returned ${res.status} ${res.statusText}`);
  return (await res.json()) as DbOverview;
}

/**
 * Step 1 of the DB-write flow: ask felix to map a natural-language instruction
 * to ONE MCP tool call, WITHOUT running it (preview-then-confirm). Returns the
 * plan to preview, or `{plan:null, reason}` when it can't be mapped.
 */
export async function planDbOperation(instruction: string): Promise<DbPlanResponse> {
  if (usingMock) return mockPlan(instruction);

  const res = await fetch(`${API_URL}/db/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Backend returned ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as DbPlanResponse;
}

/**
 * Step 2 of the DB-write flow: run a previewed plan against the cluster over
 * MCP. Resolves with the run result even on a tool-level failure (`ok:false`);
 * only throws on transport / config (non-200) errors.
 */
export async function executeDbOperation(plan: DbPlan): Promise<DbExecuteResult> {
  if (usingMock) return mockExecute(plan);

  const res = await fetch(`${API_URL}/db/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool: plan.tool, args: plan.args }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Backend returned ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as DbExecuteResult;
}

// ── mock (no backend) ─────────────────────────────────────────────────────────

function mockDbOverview(): Promise<DbOverview> {
  return Promise.resolve({
    connected: true,
    source: "cockroachdb-cloud-mcp (mock)",
    cluster: {
      id: "bea05a4f-e949-41b7-843e-10f7340ed586",
      name: "felix-db",
      cockroach_version: "v26.2.5",
      cloud_provider: "AWS",
      state: "CREATED",
      plan: "BASIC",
      regions: [{ name: "us-east-1", node_count: 0 }],
      created_at: "2026-08-15T17:38:37Z",
      updated_at: "2026-08-15T17:38:39Z",
    },
    databases: [{ database_name: "defaultdb", owner: "root", regions: [] }],
    tables_by_db: {
      defaultdb: [
        { schema_name: "public", table_name: "incidents", type: "table", estimated_row_count: 14 },
        { schema_name: "public", table_name: "resolution_steps", type: "table", estimated_row_count: 50 },
        { schema_name: "public", table_name: "doc_chunks", type: "table", estimated_row_count: 15 },
        { schema_name: "public", table_name: "code_nodes", type: "table", estimated_row_count: 42 },
        { schema_name: "public", table_name: "code_edges", type: "table", estimated_row_count: 22 },
        { schema_name: "public", table_name: "code_changes", type: "table", estimated_row_count: 11 },
        { schema_name: "public", table_name: "agent_actions", type: "table", estimated_row_count: 0 },
        { schema_name: "public", table_name: "active_incidents", type: "table", estimated_row_count: 0 },
        { schema_name: "public", table_name: "active_incident_turns", type: "table", estimated_row_count: 0 },
        { schema_name: "public", table_name: "metrics", type: "table", estimated_row_count: 0 },
      ],
    },
    running_queries: [],
    tools_used: ["get_cluster", "list_databases", "list_tables", "show_running_queries"],
  });
}

// A tiny keyword-driven stand-in for the LLM planner, so the preview/confirm
// flow is demoable with no backend. Recognizes "table"/"database"/"insert",
// otherwise declines — mirroring the real planner's {plan:null, reason} path.
function mockPlan(instruction: string): Promise<DbPlanResponse> {
  const text = instruction.toLowerCase();
  if (text.includes("database")) {
    return Promise.resolve({
      plan: {
        tool: "create_database",
        args: { name: "felix_scratch" },
        explanation: "Create a new database named felix_scratch.",
        write: true,
      },
    });
  }
  if (text.includes("insert") || text.includes("add a row") || text.includes("add row")) {
    return Promise.resolve({
      plan: {
        tool: "insert_rows",
        args: { query: "INSERT INTO defaultdb.public.oncall_schedule (engineer, week) VALUES ('mock', 1)" },
        explanation: "Insert one sample row into oncall_schedule.",
        write: true,
      },
    });
  }
  if (text.includes("table") || text.includes("create")) {
    return Promise.resolve({
      plan: {
        tool: "create_table",
        args: {
          ddl:
            "CREATE TABLE defaultdb.public.oncall_schedule (\n" +
            "  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n" +
            "  engineer STRING NOT NULL,\n" +
            "  week INT NOT NULL,\n" +
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n" +
            ")",
        },
        explanation: "Create an oncall_schedule table with a primary key and a created_at column.",
        write: true,
      },
    });
  }
  return Promise.resolve({
    plan: null,
    reason: "(mock) Couldn't map that request — try phrasing it as 'add a table for …'.",
  });
}

function mockExecute(plan: DbPlan): Promise<DbExecuteResult> {
  return Promise.resolve({
    ok: true,
    tool: plan.tool,
    args: plan.args,
    result: { status: "ok", note: "(mock) not actually executed" },
  });
}
