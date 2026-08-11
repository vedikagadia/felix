"""Given an alert, assemble the evidence felix would reason over.

This is the retrieval half of the agent loop — everything that happens BEFORE
the LLM. Takes an alert string and gathers:
  1. semantically similar past incidents          (episodic memory)
  2. relevant documentation                        (project docs)
  3. recent code changes                           (the "what changed?" signal)
  4. optionally, an upstream graph trace from the node where the symptom
     originates, to find who up the call stack could be the real cause.

The reasoning step (hand this packet to Bedrock/Claude and get a diagnosis) is
NOT here yet — this prints the packet so you can see what the agent would see.

    python -m src.incidentmemory.respond "checkout failing, db.pool.exhausted during spike"
    python -m src.incidentmemory.respond "..." --origin-node ConnectionPool.acquire
"""

from __future__ import annotations

import argparse

from . import db, embeddings


def gather(alert: str, origin_node: str | None = None, k: int = 3) -> dict:
    conn = db.get_conn()
    try:
        qv = embeddings.embed(alert)
        packet = {
            "incidents": db.recall_incidents(conn, qv, k=k),
            "docs": db.recall_docs(conn, qv, k=k),
            "changes": db.recall_changes(conn, qv, k=k, since_days=14),
            "upstream": db.graph_upstream_callers(conn, origin_node, max_depth=4) if origin_node else [],
        }
        return packet
    finally:
        conn.close()


def _print(alert: str, packet: dict) -> None:
    print("=" * 72)
    print(f"ALERT: {alert}")
    print("=" * 72)

    print("\n[1] SIMILAR PAST INCIDENTS (episodic memory)")
    for r in packet["incidents"]:
        # (id, title, severity, symptoms, root_cause, service, distance)
        print(f"  {r[6]:.3f}  [{r[2]}] {r[1]}")

    print("\n[2] RELEVANT DOCS")
    for r in packet["docs"]:
        # (id, doc_title, heading, body, doc_type, distance)
        print(f"  {r[5]:.3f}  {r[1]} — {r[2]}")

    print("\n[3] RECENT CODE CHANGES (last 14 days)")
    if not packet["changes"]:
        print("  (none in window)")
    for r in packet["changes"]:
        # (id, commit_sha, merged_at, title, summary, distance)
        print(f"  {r[5]:.3f}  {r[2].date()}  {r[3]}")

    if packet["upstream"]:
        print("\n[4] UPSTREAM CALL TRACE (symptom origin -> who drives it)")
        for r in packet["upstream"]:
            # (depth, id, name, kind, file, service, source, summary, last_commit, updated_at)
            print(f"  depth {r[0]}  {r[2]:28} {r[4]}")

    print("\n" + "-" * 72)
    print("NEXT (not yet built): hand this packet to the reasoning model for a")
    print("diagnosis + proposed resolution, then write the outcome back to memory.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Assemble felix's evidence packet for an alert.")
    ap.add_argument("alert", help="the alert / error message text")
    ap.add_argument("--origin-node", help="code_nodes.name where the symptom surfaces (enables the upstream graph trace)")
    ap.add_argument("-k", type=int, default=3, help="results per source")
    args = ap.parse_args()

    packet = gather(args.alert, origin_node=args.origin_node, k=args.k)
    _print(args.alert, packet)


if __name__ == "__main__":
    main()
