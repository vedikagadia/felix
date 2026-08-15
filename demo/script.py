"""The demo script — the ONE source of truth for the video's narration.

Each `Scene` is a beat of the story: a `key` the browser driver signals when it
reaches that beat, and the `narration` the TTS voice speaks over it. The driver
(demo/driver.py) records WHEN each beat actually happened (gated on real UI
state, never fixed sleeps); the audio builder (demo/audio.py) turns each
`narration` into a TTS clip; the muxer (demo/orchestrate.py) lays clip N onto
the recorded timeline at beat N's timestamp. That decoupling is why the video
stays in sync no matter how long recall / the model / the DB actually take.

Some beats are NARRATION-ONLY — they play over a view already on screen (e.g.
`setup` over the empty chat, `cdc_setup` while the watcher warms up). The driver
marks those too, so the voice has a slot; they don't require a UI action.

The narration is deliberately proof-oriented and aligned with WORLD.md, the
sample_project source, and the seed data, so every claim is verifiable:
  * Puzzle A — the pool-hold bug, proven from the call graph + the held-across-
    charge source and the pool_size=10 / exponential-backoff math.
  * Puzzle B — the p99->avg merge, proven from the change record + the fact that
    avg stays green (~258ms) while p99 sits on a spike (~2000ms).
  * CDC — the sinkless CHANGEFEED -> watcher trip rule (p99>=1000 AND avg<=300),
    diagnosed by the SAME loop, with no human and no dashboard alert.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scene:
    key: str
    """Stable id the driver emits (as a beat marker) when this scene begins."""
    narration: str
    """What the TTS voice says over this scene."""


# The scenes, in play order. The driver walks these keys top-to-bottom.
SCENES: list[Scene] = [
    Scene(
        "intro",
        "This is felix — an on-call incident-response agent whose entire value "
        "comes from memory. When an alert fires, felix recalls similar past "
        "incidents, relevant docs, and recent code changes by meaning, traces "
        "the code graph from where a symptom surfaced up to where the cause "
        "lives, and proposes a diagnosis.",
    ),
    Scene(
        "setup",
        "To prove that, we built a realistic checkout service with two bugs "
        "planted in it. Each bug is designed so that only one kind of memory can "
        "solve it — that's how we show every memory source pulls its weight. "
        "Let's watch felix diagnose both, and check its reasoning against the "
        "actual code.",
    ),
    Scene(
        "puzzle_a_submit",
        "First alert: checkout is failing with database pool exhaustion during a "
        "traffic spike. The on-call instinct is that the database is out of "
        "capacity — so you scale the database. But scaling it doesn't help, and "
        "the errors keep coming.",
    ),
    Scene(
        "puzzle_a_diagnosis",
        "felix rejects the obvious read. Its diagnosis: this isn't a database "
        "capacity problem at all. The checkout handler acquires a pooled "
        "connection at the very start of a request and holds it across the "
        "payment call — so the pool, not the database, is the bottleneck.",
    ),
    Scene(
        "puzzle_a_graph",
        "Here's the proof, and why only the code graph can find it. felix traced "
        "the graph upstream — with a recursive query in CockroachDB — from "
        "ConnectionPool.acquire, where the error surfaced, up to "
        "CheckoutHandler.process, and read its source. The connection is "
        "acquired up front and released only in the finally block, after the "
        "payment charge returns. And that charge retries a slow gateway with "
        "exponential backoff — half a second, one, two, four seconds — so a "
        "single slow checkout pins one of just ten pool connections for many "
        "seconds. Under load the pool drains, and unrelated requests fail. No "
        "past incident, doc, or merge mentions this — only reading how the code "
        "connects reveals it. That's the first CockroachDB feature: recursive "
        "graph traversal.",
    ),
    Scene(
        "puzzle_b_submit",
        "Second alert, and a harder one: customers report slow checkout, but "
        "every dashboard is green and no alert ever fired. There's no error to "
        "grep for and nothing in the code graph looks wrong.",
    ),
    Scene(
        "puzzle_b_diagnosis",
        "felix finds it in the recent merges. Seven days ago a commit switched "
        "the checkout latency metric from p99 to average, in metrics dot py. "
        "Here's why that hides the bug: with a few slow requests per minute, the "
        "ninety-ninth percentile sits on a two-thousand-millisecond spike, but "
        "the average of the same window is only about two-hundred-sixty "
        "milliseconds — comfortably in the green. So the dashboard looks "
        "healthy, the p99 alert never fires, and real users still hit the tail. "
        "Notice metrics dot py isn't even on the checkout call graph — a "
        "graph-based search would miss it entirely. Only semantic vector search "
        "over CockroachDB's native vector type, filtered to recent changes, "
        "surfaces that one commit. That's the second feature.",
    ),
    Scene(
        "cdc_setup",
        "Now the real-time path. The checkout service emits a latency sample on "
        "every request into a metrics table. A sinkless CockroachDB changefeed "
        "streams each new row to a felix watcher the instant it's written, and "
        "the watcher keeps a rolling sixty-sample window. Its trip rule is "
        "exactly the puzzle-two signature: fire only when p99 is above one "
        "second while the average stays under three hundred milliseconds — a bad "
        "tail hiding behind a healthy average. No dashboard shows it, and no "
        "human is watching.",
    ),
    Scene(
        "cdc_alert",
        "There it is — felix raised this alert on its own. The changefeed "
        "delivered a burst of tail-latency spikes, the watcher's window crossed "
        "the threshold while the average stayed green, and felix opened the "
        "incident with no human in the loop and no dashboard alert to prompt it.",
    ),
    Scene(
        "cdc_diagnosis",
        "One click opens the incident felix already diagnosed — the same loop, "
        "recall, reasoning, and write-back, this time triggered by the data "
        "itself. So: two CockroachDB features on the live path — recursive graph "
        "traversal and native vector search — AWS Bedrock for reasoning, and "
        "memory that makes felix sharper with every incident it sees. That's "
        "felix.",
    ),
]

# Beats that are narration over an existing view (no UI action drives them). The
# driver marks these against whatever is already on screen.
NARRATION_ONLY = {"setup", "cdc_setup"}


def scene(key: str) -> Scene:
    """Look up a scene by key (raises if the driver and script drift apart)."""
    for s in SCENES:
        if s.key == key:
            return s
    raise KeyError(f"no scene with key {key!r} (demo/script.py and driver disagree)")
