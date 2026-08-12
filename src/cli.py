"""felix command-line entry point.

    python -m src respond "checkout failing, db.pool.exhausted during spike"
    python -m src respond "..." --origin-node ConnectionPool.acquire
    python -m src respond "..." --no-llm
    python -m src seed --truncate
    python -m src parse
    python -m src mcp-probe
    python -m src serve --reload

`respond` assembles the evidence packet felix reasons over (the retrieval half
of the agent loop — everything BEFORE the LLM), prints it, then hands it to the
LLM reasoning step (IncidentDiagnoser) for a diagnosis + proposed resolution,
printed as block [5]. `--no-llm` stops after the evidence packet and makes no
DB writes. `serve` exposes the same loop over HTTP for the frontend (see
src/api/app.py).
"""

from __future__ import annotations

import argparse

from .config import get_settings
from .models import Diagnosis, EvidencePacket
from .seed import loader
from .seed.parser import SERVICE_NAME, parse_project
from .service.evidence_gatherer import EvidenceGatherer
from .store.connection import get_conn


# ── respond ───────────────────────────────────────────────────────────────────


def _print_packet(packet: EvidencePacket) -> None:
    print("=" * 72)
    print(f"ALERT: {packet.alert}")
    print("=" * 72)

    print("\n[1] SIMILAR PAST INCIDENTS (episodic memory)")
    for r in packet.incidents:
        inc = r.item
        print(f"  {r.distance:.3f}  [{inc.severity}] {inc.title}")

    print("\n[2] RELEVANT DOCS")
    for r in packet.docs:
        doc = r.item
        print(f"  {r.distance:.3f}  {doc.doc_title} — {doc.heading}")

    print("\n[3] RECENT CODE CHANGES (last 14 days)")
    if not packet.changes:
        print("  (none in window)")
    for r in packet.changes:
        chg = r.item
        print(f"  {r.distance:.3f}  {chg.merged_at.date()}  {chg.title}")

    if packet.upstream:
        print("\n[4] UPSTREAM CALL TRACE (symptom origin -> who drives it)")
        for hit in packet.upstream:
            print(f"  depth {hit.depth}  {hit.node.name:28} {hit.node.file}")


def _print_diagnosis(diagnosis: Diagnosis) -> None:
    print("\n[5] DIAGNOSIS")
    print(f"  summary:    {diagnosis.summary}")
    print(f"  root_cause: {diagnosis.root_cause}")
    print("  proposed_steps:")
    if not diagnosis.proposed_steps:
        print("    (none)")
    for step in diagnosis.proposed_steps:
        print(f"    - {step}")
    print(f"  cited_incident_ids: {diagnosis.cited_incident_ids}")
    print(f"  cited_change_ids:   {diagnosis.cited_change_ids}")
    print(f"  confidence: {diagnosis.confidence}")


def _cmd_respond(args: argparse.Namespace) -> None:
    conn = get_conn()
    try:
        gatherer = EvidenceGatherer(conn)
        settings = get_settings()
        llm_unavailable = settings.llm_provider == "gemini" and not settings.gemini_api_key

        # Retrieval-only paths (--no-llm, or no LLM key): gather + print, no LLM,
        # no writes.
        if args.no_llm or llm_unavailable:
            packet = gatherer.gather(args.alert, origin_node=args.origin_node, k=args.k)
            _print_packet(packet)
            if args.no_llm:
                print("\n" + "-" * 72)
                print("(--no-llm: stopped at the evidence packet; no diagnosis, no DB writes)")
            else:
                print("\n[5] DIAGNOSIS")
                print("  (skipped: GEMINI_API_KEY is not set — evidence packet only)")
            return

        # Full path: respond() gathers ONCE and returns the packet it reasoned
        # over together with the diagnosis (no second embed/recall).
        from .clients.llm import get_llm
        from .service.diagnoser import IncidentDiagnoser
        from .store.repositories import ActionRepository, IncidentRepository

        diagnoser = IncidentDiagnoser(
            gatherer, get_llm(), IncidentRepository(conn), ActionRepository(conn)
        )
        result = diagnoser.respond(args.alert, origin_node=args.origin_node, k=args.k)
        _print_packet(result.evidence)
        _print_diagnosis(result.diagnosis)
    finally:
        conn.close()


# ── seed ───────────────────────────────────────────────────────────────────────


def _cmd_seed(args: argparse.Namespace) -> None:
    counts = loader.run(apply_schema_first=args.apply_schema, truncate=args.truncate)
    print("seeded:")
    for k, v in counts.items():
        print(f"  {k:14} {v}")


# ── parse ──────────────────────────────────────────────────────────────────────


def _cmd_parse(args: argparse.Namespace) -> None:
    nodes, edges = parse_project(str(loader.SAMPLE_ROOT))
    id_to_name = {n["id"]: n["name"] for n in nodes}
    print(f"=== code graph for {SERVICE_NAME} ===")
    print(f"nodes: {len(nodes)}  edges: {len(edges)}")
    by_kind: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    print("node counts by kind:", by_kind)
    print("\n-- edges --")
    for e in edges:
        print(f"{id_to_name.get(e['src_id'])} --{e['kind']}--> {id_to_name.get(e['dst_id'])}")


# ── mcp-probe ────────────────────────────────────────────────────────────────


def _cmd_mcp_probe(args: argparse.Namespace) -> None:
    """Connect to the CockroachDB Managed MCP Server and list the tools it
    advertises — the discovery spike, made runnable. Requires CRDB_MCP_URL /
    CRDB_MCP_API_KEY (Cloud Console MCP config); prints a clear message rather
    than a traceback when they're unset."""
    import asyncio

    from .clients import cockroach_mcp

    settings = get_settings()
    if not settings.crdb_mcp_url or not settings.crdb_mcp_api_key:
        print("mcp-probe: CRDB_MCP_URL / CRDB_MCP_API_KEY are not set.")
        print("  Set them from the Cloud Console MCP config snippet, then re-run.")
        return

    async def _probe() -> None:
        async with cockroach_mcp.connect() as session:
            tools = await cockroach_mcp.list_tools(session)
            print(f"Connected to {settings.crdb_mcp_url}. {len(tools)} tool(s):")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")

    asyncio.run(_probe())


# ── serve ────────────────────────────────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> None:
    """Launch the HTTP API (the same recall + reasoning loop as `respond`,
    exposed over POST /chat and /recall for the frontend). Requires the `api`
    extras — `pip install fastapi 'uvicorn[standard]'`."""
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("serve: uvicorn is not installed. Run: pip install fastapi 'uvicorn[standard]'")
        return

    # Pass the app as an import string so --reload can re-import on edits.
    uvicorn.run(
        "src.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


# ── arg parsing ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="felix", description="felix — memory-driven incident-response agent")
    sub = ap.add_subparsers(dest="command", required=True)

    p_respond = sub.add_parser("respond", help="assemble felix's evidence packet for an alert")
    p_respond.add_argument("alert", help="the alert / error message text")
    p_respond.add_argument(
        "--origin-node",
        help="code_nodes.name where the symptom surfaces (enables the upstream graph trace)",
    )
    p_respond.add_argument("-k", type=int, default=3, help="results per source")
    p_respond.add_argument(
        "--no-llm",
        action="store_true",
        help="evidence packet only ([1]-[4]) — skip the LLM diagnosis step, no DB writes",
    )
    p_respond.set_defaults(func=_cmd_respond)

    p_seed = sub.add_parser("seed", help="seed CockroachDB with felix's memory corpora")
    p_seed.add_argument("--apply-schema", action="store_true", help="run sql/schema.sql first (idempotent)")
    p_seed.add_argument("--truncate", action="store_true", help="clear incidents/docs/code_changes before seeding")
    p_seed.set_defaults(func=_cmd_seed)

    p_parse = sub.add_parser("parse", help="parse the sample project into a code graph and print a summary")
    p_parse.set_defaults(func=_cmd_parse)

    p_mcp = sub.add_parser("mcp-probe", help="connect to the CockroachDB Managed MCP Server and list its tools")
    p_mcp.set_defaults(func=_cmd_mcp_probe)

    p_serve = sub.add_parser("serve", help="run the HTTP API (POST /chat, /recall) for the frontend")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    p_serve.add_argument("--reload", action="store_true", help="autoreload on code changes (dev)")
    p_serve.set_defaults(func=_cmd_serve)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
