"""felix command-line entry point.

    python -m src respond "checkout failing, db.pool.exhausted during spike"
    python -m src respond "..." --origin-node ConnectionPool.acquire
    python -m src seed --truncate
    python -m src parse

`respond` assembles the evidence packet felix would reason over (the retrieval
half of the agent loop — everything BEFORE the LLM) and prints it. The reasoning
step (hand the packet to the LLM for a diagnosis + resolution, then write the
outcome back to memory) is step 2 and not wired in here yet.
"""

from __future__ import annotations

import argparse

from .models import EvidencePacket
from .seed import loader
from .seed.parser import SERVICE_NAME, parse_project
from .service.retriever import Retriever
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

    print("\n" + "-" * 72)
    print("NEXT (not yet built): hand this packet to the reasoning model for a")
    print("diagnosis + proposed resolution, then write the outcome back to memory.")


def _cmd_respond(args: argparse.Namespace) -> None:
    conn = get_conn()
    try:
        retriever = Retriever(conn)
        packet = retriever.gather(args.alert, origin_node=args.origin_node, k=args.k)
    finally:
        conn.close()
    _print_packet(packet)


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
    p_respond.set_defaults(func=_cmd_respond)

    p_seed = sub.add_parser("seed", help="seed CockroachDB with felix's memory corpora")
    p_seed.add_argument("--apply-schema", action="store_true", help="run sql/schema.sql first (idempotent)")
    p_seed.add_argument("--truncate", action="store_true", help="clear incidents/docs/code_changes before seeding")
    p_seed.set_defaults(func=_cmd_seed)

    p_parse = sub.add_parser("parse", help="parse the sample project into a code graph and print a summary")
    p_parse.set_defaults(func=_cmd_parse)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
