"""The browser driver — records the felix UI performing the demo story.

Runs a headed-size Chromium via Playwright with video recording on, walks the
two planted puzzles and the live CDC alert, and for each story beat records the
timestamp (relative to video start) at which that beat actually happened. Every
wait is gated on REAL UI state — a diagnosis card rendering, the alert banner
appearing — never a fixed sleep, so the recording tracks the live backend no
matter how long recall / the model / the changefeed take.

Two outputs land in --out-dir:
  * a `.webm` screen recording (Playwright names it; we rename to demo.webm)
  * `beats.json` — {scene_key: start_seconds, ...} plus "video_duration"

`--dwell-json` (written by demo/audio.py from the TTS clip lengths) tells the
driver the MINIMUM time to linger on each beat so the on-screen action never
outruns the narration that will be spoken over it. Missing keys fall back to
DEFAULT_DWELL.

Run standalone for a dry run (no audio pacing):
    python -m demo.driver --url http://127.0.0.1:8000 --out-dir demo/out
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

# Alerts felix is authored to solve (WORLD.md §5). Puzzle A pins the origin node
# so the recursive graph trace runs; puzzle B is pure semantic recall.
PUZZLE_A_ALERT = "checkout failing, db.pool.exhausted during traffic spike"
PUZZLE_A_ORIGIN = "ConnectionPool.acquire"
PUZZLE_B_ALERT = "customers report slow checkout but dashboards look fine"

VIEWPORT = {"width": 1360, "height": 860}
DEFAULT_DWELL = 6.0  # seconds to linger on a beat when no TTS length is known
# A diagnosis can take a while (recall + a real LLM round-trip); be generous.
DIAGNOSIS_TIMEOUT_MS = 90_000


class Recorder:
    """Marks story beats against a monotonic clock anchored at video start."""

    def __init__(self, page: Page, dwell: dict[str, float], out_dir: Path):
        self.page = page
        self.dwell = dwell
        self.out_dir = out_dir
        self.beats: dict[str, float] = {}
        self._t0 = time.monotonic()

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def mark(self, key: str) -> None:
        """Stamp the moment this beat became visible, then linger long enough
        for its narration to play over it."""
        t = self._now()
        self.beats[key] = t
        print(f"[beat] {key:20} @ {t:6.2f}s")
        time.sleep(self.dwell.get(key, DEFAULT_DWELL))

    def finish(self, video_duration: float) -> None:
        self.beats["video_duration"] = video_duration
        (self.out_dir / "beats.json").write_text(json.dumps(self.beats, indent=2))
        print(f"[driver] wrote {self.out_dir / 'beats.json'}")


def _submit_alert(page: Page, alert: str, origin_node: str | None) -> None:
    """Type an alert into the composer (optionally pinning an origin node via the
    Advanced panel) and fire it."""
    page.fill(".composer__input", alert)
    if origin_node:
        page.click(".composer__toggle")  # reveal Advanced
        page.fill(".composer__field input", origin_node)
    page.click(".btn--send")


def _wait_for_new_diagnosis(page: Page, prior_count: int) -> None:
    """Block until one more diagnosis card than `prior_count` has rendered — i.e.
    the turn we just submitted has finished streaming and parsed."""
    page.wait_for_function(
        "n => document.querySelectorAll('.diagnosis__summary').length > n",
        arg=prior_count,
        timeout=DIAGNOSIS_TIMEOUT_MS,
    )


def _scroll_evidence_to(page: Page, text: str) -> None:
    """Bring an evidence section (by its heading text) into view in the panel."""
    loc = page.locator(".evsec__title", has_text=text).first
    if loc.count():
        loc.scroll_into_view_if_needed()


def run(url: str, out_dir: Path, dwell: dict[str, float], cdc_timeout_s: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(out_dir),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        rec = Recorder(page, dwell, out_dir)

        # ── Scene: intro + setup (both narrate over the empty chat) ───────────
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(".thread--empty h2", timeout=15_000)
        rec.mark("intro")
        rec.mark("setup")  # narration-only: still on the empty "What's on fire?" view

        # ── Scene: puzzle A (code-only) ───────────────────────────────────────
        _submit_alert(page, PUZZLE_A_ALERT, PUZZLE_A_ORIGIN)
        page.wait_for_selector(".msg--user .msg__text", timeout=10_000)
        rec.mark("puzzle_a_submit")

        _wait_for_new_diagnosis(page, prior_count=0)
        rec.mark("puzzle_a_diagnosis")

        _scroll_evidence_to(page, "Upstream call trace")
        page.wait_for_selector(".trace__source", timeout=10_000)
        rec.mark("puzzle_a_graph")

        # ── Scene: puzzle B (merge-only) ──────────────────────────────────────
        page.click(".composer__new")  # + New incident: clears the session
        _submit_alert(page, PUZZLE_B_ALERT, origin_node=None)
        rec.mark("puzzle_b_submit")

        _wait_for_new_diagnosis(page, prior_count=1)
        _scroll_evidence_to(page, "Recent code changes")
        rec.mark("puzzle_b_diagnosis")

        # ── Scene: cdc_setup (narrate the real-time path while the watcher warms
        # up — the banner appears mid-narration, which reads as cause→effect) ──
        rec.mark("cdc_setup")

        # ── Scene: live CDC alert (watcher + emitter feed this) ───────────────
        # The banner only exists once the watcher has tripped; poll for it.
        try:
            page.wait_for_selector(
                '[aria-label="Live alerts"] button',
                timeout=int(cdc_timeout_s * 1000),
            )
        except PWTimeout:
            print(
                f"[driver] no CDC alert within {cdc_timeout_s:.0f}s — is the watcher "
                "tripping? Recording the story without the live-CDC beats."
            )
            _finalize(context, browser, page, rec)
            return
        page.locator('[aria-label="Live alerts"]').scroll_into_view_if_needed()
        rec.mark("cdc_alert")

        # ── Scene: open the auto-diagnosed incident ───────────────────────────
        prior = page.locator(".diagnosis__summary").count()
        page.click('[aria-label="Live alerts"] button')
        _wait_for_new_diagnosis(page, prior_count=prior)
        rec.mark("cdc_diagnosis")

        _finalize(context, browser, page, rec)


def _finalize(context, browser, page: Page, rec: Recorder) -> None:
    """Close the context (flushes the video), rename it to demo.webm, and write
    beats.json stamped with the real recorded duration."""
    duration = rec._now()
    video = page.video
    context.close()  # flushes + finalizes the webm
    browser.close()
    if video is not None:
        src = Path(video.path())
        dst = rec.out_dir / "demo.webm"
        src.replace(dst)
        print(f"[driver] recording saved to {dst}")
    rec.finish(video_duration=duration)


def main() -> None:
    ap = argparse.ArgumentParser(description="Record the felix demo in a browser.")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="app origin (serve)")
    ap.add_argument("--out-dir", default="demo/out", help="where to write demo.webm + beats.json")
    ap.add_argument("--dwell-json", help="scene->seconds min-dwell map (from demo/audio.py)")
    ap.add_argument("--cdc-timeout", type=float, default=120.0, help="max wait for the CDC alert banner")
    args = ap.parse_args()

    dwell: dict[str, float] = {}
    if args.dwell_json and Path(args.dwell_json).is_file():
        dwell = json.loads(Path(args.dwell_json).read_text())

    run(args.url, Path(args.out_dir), dwell, args.cdc_timeout)


if __name__ == "__main__":
    main()
