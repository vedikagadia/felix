# felix demo — runbook

The exact story the video tells, beat by beat. `demo/orchestrate.py` performs
this automatically; this doc is the human-readable script (for narration review,
and so you can drive it by hand if the automation ever needs babysitting).

The narration text lives in `demo/script.py` (`SCENES`) — that's the single
source of truth. Edit it there, not here; re-run `python -m demo.orchestrate`
and the voice + pacing update themselves.

---

## Story arc (~90–120s)

Two planted puzzles that each prove one memory source pulls its weight, then the
live changefeed that shows felix acting on its own.

| # | Beat (`scene.key`) | On screen | What the voice says (gist) |
|---|---|---|---|
| 1 | `intro` | Empty chat, "What's on fire?" | felix = an incident agent whose value is memory. |
| 2 | `puzzle_a_submit` | Type alert A + origin node `ConnectionPool.acquire`, hit Diagnose | Looks like a DB-capacity problem; scaling doesn't help. |
| 3 | `puzzle_a_diagnosis` | Diagnosis card renders | Real cause is in the code: connection held across the payment retry loop. |
| 4 | `puzzle_a_graph` | Evidence panel scrolls to the **upstream call trace** (with source) | Only the graph solves this — **recursive-CTE traversal in CockroachDB** (offering #1). |
| 5 | `puzzle_b_submit` | + New incident, type alert B, Diagnose | Slow checkout, green dashboards, no alert fired. |
| 6 | `puzzle_b_diagnosis` | Diagnosis + **Recent code changes** card | A merge switched p99→avg, hiding the tail — surfaced by **native VECTOR search** (offering #2). |
| 7 | `cdc_alert` | The **live alert banner** appears on its own | A CockroachDB **changefeed** streams metrics; p99 spiked while avg stayed flat → felix raised it itself. |
| 8 | `cdc_diagnosis` | Click the alert → the pre-diagnosed incident opens | Same recall→reason→write-back loop, triggered by the data. Two CRDB offerings + AWS Bedrock + memory. |

---

## The two planted puzzles (why each is here)

- **Puzzle A — code-only.** Alert: `checkout failing, db.pool.exhausted during
  traffic spike`, origin node `ConnectionPool.acquire`. No incident/doc/change
  reveals the cause; **only the code graph** does (trace upstream to
  `CheckoutHandler.process`, which holds the pooled connection across
  `PaymentClient.charge`'s retry loop). This is the recursive-traversal beat.
- **Puzzle B — merge-only.** Alert: `customers report slow checkout but
  dashboards look fine`. No origin node. **Only the `code_changes` record**
  reveals it — a merge flipped `LATENCY_AGGREGATION` from `p99` to `avg`. This is
  the semantic-vector-search beat.

## The CDC beat (the climax)

The sample emitter (`sample_project.run`) writes `checkout_latency_ms` samples —
mostly ~100ms, a periodic spike to 1500–2500ms. Over a 60-sample window that
puts p99 > 1000ms while the mean stays < 300ms. The watcher (`src watch`) holds
a **sinkless CHANGEFEED** on `metrics`, detects that signature, and calls the
same diagnoser — opening an incident **with no human and no dashboard alert**.
The browser polls `/alerts`, the banner appears, and clicking it opens the
session felix already diagnosed.

The orchestrator resets CDC state before recording (`UPDATE active_incidents SET
status='resolved' WHERE source='cdc'; TRUNCATE metrics;`) so a *fresh* alert
fires during the take.

---

## Driving it by hand (fallback)

If you ever need to record manually instead of via Playwright:

1. `python -m src serve` (after `npm run build` in `frontend/`), open the URL.
2. Terminal 2: `python -m src watch --debug`. Terminal 3:
   `python -m sample_project.run --interval 0.4`.
3. Reset CDC state (SQL above) before you start so the banner fires fresh.
4. Screen-record while you walk beats 1–8 in order. Linger on each diagnosis and
   on the evidence panel long enough to read it.
5. Generate narration with `python -m demo.audio`; the clip lengths in
   `demo/out/audio/dwell.json` tell you how long to hold each beat.
