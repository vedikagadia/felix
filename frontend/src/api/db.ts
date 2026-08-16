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
import type { DbOverview } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;

export async function fetchDbOverview(): Promise<DbOverview> {
  if (usingMock) return mockDbOverview();

  const res = await fetch(`${API_URL}/db/overview`);
  if (!res.ok) throw new Error(`Backend returned ${res.status} ${res.statusText}`);
  return (await res.json()) as DbOverview;
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
