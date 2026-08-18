import { useEffect, useState } from "react";
import type { DbExecuteResult, DbOverview, DbPlan, DbTable } from "../api/types";
import { executeDbOperation, fetchDbOverview, planDbOperation } from "../api/db";

/**
 * DB overview — a read-only view of the CockroachDB cluster, fetched from
 * `GET /db/overview`, which the backend gathers entirely through the
 * **CockroachDB Cloud Managed MCP Server** (felix as its own MCP client). This
 * is felix exercising MCP as a live CockroachDB tool: cluster metadata,
 * databases, tables + row counts, and any running queries — all over MCP, no
 * direct SQL connection on this path.
 */

function fmtCount(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

export function DbOverviewPage() {
  const [data, setData] = useState<DbOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    setError(null);
    fetchDbOverview()
      .then((d) => setData(d))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  return (
    <div className="dbov">
      <div className="dbov__head">
        <h2>
          DB overview
          <span className="dbov__via" title="Data gathered via the CockroachDB Cloud Managed MCP Server">
            via CockroachDB MCP
          </span>
        </h2>
        <p className="dbov__sub">
          A read-only snapshot of the cluster, gathered through the{" "}
          <strong className="dbov__mcp">CockroachDB Cloud Managed MCP Server</strong> — felix acting
          as its own MCP client. No direct SQL on this path; every number below came back from an
          MCP tool call.
        </p>
      </div>

      {loading && <div className="dbov__status">Querying the cluster over MCP…</div>}
      {error && !loading && (
        <div className="error dbov__error">
          Couldn’t load the overview: {error}
          <button type="button" className="dbov__retry" onClick={load}>
            retry
          </button>
        </div>
      )}

      {!loading && !error && data && !data.connected && (
        <div className="dbov__disconnected">
          <strong>MCP not connected.</strong>
          <p>{data.reason ?? "The CockroachDB MCP server is unavailable."}</p>
          <p className="dbov__hint">
            Authenticate felix once with <code>python -m src mcp-probe</code>, then{" "}
            <button type="button" className="dbov__retry" onClick={load}>
              retry
            </button>
            .
          </p>
        </div>
      )}

      {!loading && !error && data && data.connected && (
        <>
          <DbWriteBox onExecuted={load} />
          {data.cluster && <ClusterCard cluster={data.cluster} toolsUsed={data.tools_used} />}
          <DatabasesSection data={data} />
          <RunningQueries queries={data.running_queries ?? []} />
        </>
      )}
    </div>
  );
}

/**
 * "Ask felix to change the DB" — a natural-language box that maps a request to
 * ONE CockroachDB MCP tool call, previews it, and runs it only after the
 * operator confirms (preview-then-confirm). The server exposes only additive
 * write tools (create/insert), so the worst case is a new table or extra rows.
 * On a successful run it refreshes the overview via `onExecuted`.
 */
function DbWriteBox({ onExecuted }: { onExecuted: () => void }) {
  const [instruction, setInstruction] = useState("");
  const [plan, setPlan] = useState<DbPlan | null>(null);
  const [declineReason, setDeclineReason] = useState<string | null>(null);
  const [planning, setPlanning] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DbExecuteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setPlan(null);
    setDeclineReason(null);
    setResult(null);
    setError(null);
  }

  function onPlan() {
    if (!instruction.trim()) return;
    reset();
    setPlanning(true);
    planDbOperation(instruction.trim())
      .then((res) => {
        if (res.plan) setPlan(res.plan);
        else setDeclineReason(res.reason ?? "felix couldn't map that request to a safe operation.");
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setPlanning(false));
  }

  function onRun() {
    if (!plan) return;
    setRunning(true);
    setError(null);
    executeDbOperation(plan)
      .then((res) => {
        setResult(res);
        if (res.ok) onExecuted(); // refresh the overview so the new table/rows show
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setRunning(false));
  }

  const arg = plan ? Object.entries(plan.args)[0] : null;

  return (
    <article className="dbcard dbwrite">
      <header className="dbcard__head">
        <div>
          <span className="dbcard__title">Ask felix to change the DB</span>
          <span className="dbcard__sub">
            natural language → one MCP tool call · you review it before it runs
          </span>
        </div>
        <span className="badge badge--accent">via CockroachDB MCP</span>
      </header>

      <div className="dbwrite__composer">
        <textarea
          className="dbwrite__input"
          rows={2}
          value={instruction}
          placeholder="e.g. add a table for on-call schedules with engineer and week"
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onPlan();
          }}
          disabled={planning || running}
        />
        <button
          type="button"
          className="dbwrite__plan"
          onClick={onPlan}
          disabled={planning || running || !instruction.trim()}
        >
          {planning ? "Planning…" : "Plan it"}
        </button>
      </div>

      {declineReason && !plan && (
        <p className="dbwrite__decline">{declineReason}</p>
      )}

      {error && <p className="dbwrite__error">{error}</p>}

      {plan && (
        <div className="dbwrite__preview">
          <div className="dbwrite__previewhead">
            <span className={`dbwrite__tool ${plan.write ? "is-write" : "is-read"}`}>
              {plan.tool}
            </span>
            <span className="dbwrite__toolkind">{plan.write ? "write" : "read-only"}</span>
          </div>
          {plan.explanation && <p className="dbwrite__explain">{plan.explanation}</p>}
          {arg && (
            <pre className="dbwrite__sql">
              <span className="dbwrite__argkey">{arg[0]}:</span> {arg[1]}
            </pre>
          )}

          {!result && (
            <div className="dbwrite__actions">
              <button type="button" className="dbwrite__run" onClick={onRun} disabled={running}>
                {running ? "Running…" : plan.write ? "Run it" : "Run query"}
              </button>
              <button
                type="button"
                className="dbwrite__cancel"
                onClick={reset}
                disabled={running}
              >
                Cancel
              </button>
            </div>
          )}

          {result && (
            <div className={`dbwrite__result ${result.ok ? "is-ok" : "is-err"}`}>
              <strong>{result.ok ? "✓ Executed over MCP" : "✗ MCP returned an error"}</strong>
              <pre>{JSON.stringify(result.ok ? result.result : result.error, null, 2)}</pre>
              <button type="button" className="dbwrite__cancel" onClick={reset}>
                Done
              </button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function ClusterCard({
  cluster,
  toolsUsed,
}: {
  cluster: NonNullable<DbOverview["cluster"]>;
  toolsUsed?: string[];
}) {
  return (
    <article className="dbcard dbcard--cluster">
      <header className="dbcard__head">
        <div>
          <span className="dbcard__title">{cluster.name}</span>
          <span className="dbcard__sub">
            {cluster.cloud_provider} · {cluster.plan} · {cluster.state}
          </span>
        </div>
        <span className="badge badge--ok">{cluster.cockroach_version}</span>
      </header>

      <dl className="dbcard__facts">
        <div>
          <dt>Regions</dt>
          <dd>{cluster.regions.map((r) => `${r.name} (${r.node_count})`).join(", ") || "—"}</dd>
        </div>
        <div>
          <dt>Cluster id</dt>
          <dd className="dbcard__mono">{cluster.id}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{new Date(cluster.created_at).toLocaleString()}</dd>
        </div>
      </dl>

      {toolsUsed && toolsUsed.length > 0 && (
        <div className="dbcard__tools">
          <span className="dbcard__toolslabel">MCP tools invoked</span>
          {toolsUsed.map((t) => (
            <code key={t} className="dbcard__tool">
              {t}
            </code>
          ))}
        </div>
      )}
    </article>
  );
}

function DatabasesSection({ data }: { data: DbOverview }) {
  const tablesByDb = data.tables_by_db ?? {};
  return (
    <div className="dbov__dbs">
      {(data.databases ?? []).map((db) => {
        const tables = tablesByDb[db.database_name] ?? [];
        const totalRows = tables.reduce((a, t) => a + (t.estimated_row_count ?? 0), 0);
        return (
          <article className="dbcard" key={db.database_name}>
            <header className="dbcard__head">
              <div>
                <span className="dbcard__title">{db.database_name}</span>
                <span className="dbcard__sub">
                  {tables.length} table{tables.length === 1 ? "" : "s"} · ~{fmtCount(totalRows)} rows
                  {db.owner ? ` · owner ${db.owner}` : ""}
                </span>
              </div>
            </header>
            {tables.length > 0 ? (
              <TableList tables={tables} />
            ) : (
              <p className="dbcard__empty">No tables.</p>
            )}
          </article>
        );
      })}
    </div>
  );
}

function TableList({ tables }: { tables: DbTable[] }) {
  const max = Math.max(1, ...tables.map((t) => t.estimated_row_count ?? 0));
  return (
    <table className="dbtable">
      <thead>
        <tr>
          <th>Table</th>
          <th>Schema</th>
          <th className="dbtable__num">Est. rows</th>
        </tr>
      </thead>
      <tbody>
        {tables.map((t) => {
          const rows = t.estimated_row_count ?? 0;
          return (
            <tr key={`${t.schema_name}.${t.table_name}`}>
              <td className="dbtable__name">{t.table_name}</td>
              <td className="dbtable__schema">{t.schema_name}</td>
              <td className="dbtable__num">
                <span className="dbtable__bar" style={{ width: `${(rows / max) * 100}%` }} />
                <span className="dbtable__count">{fmtCount(t.estimated_row_count)}</span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function RunningQueries({ queries }: { queries: Array<Record<string, unknown>> }) {
  return (
    <article className="dbcard">
      <header className="dbcard__head">
        <div>
          <span className="dbcard__title">Running queries</span>
          <span className="dbcard__sub">live from the cluster (show_running_queries)</span>
        </div>
        <span className={`badge ${queries.length ? "badge--alarm" : "badge--ok"}`}>
          {queries.length} active
        </span>
      </header>
      {queries.length === 0 ? (
        <p className="dbcard__empty">No queries currently executing.</p>
      ) : (
        <ul className="dbov__queries">
          {queries.map((q, i) => (
            <li key={i} className="dbov__query">
              <code>{String(q.query ?? q.statement ?? JSON.stringify(q))}</code>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
