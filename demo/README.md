# demo/ — the automated demo-video pipeline

One command turns the live felix stack into a narrated `.mp4`:

```bash
python -m demo.orchestrate
# → demo/out/felix-demo.mp4
```

It records the **real** app (real vector search, real recursive-CTE graph
trace, a real CockroachDB changefeed, real Bedrock/Gemini reasoning) driving
itself through both planted puzzles and the live CDC alert, then lays a
text-to-speech voiceover onto the recording — in sync, because narration is
pinned to when each on-screen beat actually happened, not to a fixed clock.

## What it produces

```
demo/out/
  felix-demo.mp4      ← the finished video (share this)
  demo.webm           ← the raw screen recording
  beats.json          ← {scene: seconds} the narration was synced against
  audio/*.wav         ← per-scene TTS clips
  serve.log watch.log emitter.log   ← the backend processes' output
```

## Prerequisites

Beyond the normal felix local setup (see `../SETUP.md`):

| Need | Why | Install |
|---|---|---|
| Seeded local CockroachDB, running | the app records against it | SETUP.md §1–4 |
| Built frontend (`frontend/dist/`) | `serve` serves the UI same-origin | `cd frontend && npm run build` |
| `GEMINI_API_KEY` in `.env` | the watcher must reason on a CDC trip | SETUP.md §3 |
| `ffmpeg` (+`ffprobe`) | TTS transcode + audio/video mux | `brew install ffmpeg` |
| Playwright + Chromium | drives & records the browser | `pip install playwright && playwright install chromium` |
| macOS `say` | text-to-speech (offline, no key) | built in |

The orchestrator preflight-checks these and prints the exact fix if one's
missing, before it starts anything.

## The pieces (each runnable on its own)

| Module | Does | Standalone |
|---|---|---|
| `script.py` | the narration — **edit here** to change what's said | (data) |
| `audio.py` | TTS each scene → `*.wav` + `dwell.json` | `python -m demo.audio` |
| `driver.py` | record the browser; stamp each beat's timestamp | `python -m demo.driver --url http://127.0.0.1:8000` |
| `orchestrate.py` | do all of it: TTS → stack up → record → mux | `python -m demo.orchestrate` |
| `RUNBOOK.md` | the beat-by-beat story (+ manual fallback) | — |

## Common tweaks

- **Nicer voice:** `--voice "Ava (Premium)"` (download via System Settings →
  Accessibility → Spoken Content → System Voice), or any name from `say -v '?'`.
- **Change the narration:** edit `SCENES` in `script.py`, re-run orchestrate.
- **Iterate on video only:** `python -m demo.orchestrate --skip-audio` reuses
  the existing TTS clips.
- **Trip the CDC alert sooner:** `--interval 0.3` (faster emitter). If the
  watcher hasn't tripped within `--cdc-timeout` (default 120s), the video is
  still produced — just without the two live-CDC beats.

## Why it stays in sync

The driver never sleeps a fixed amount waiting for the backend. It blocks on
real UI state — "a new diagnosis card exists", "the alert banner appeared" — and
records the wall-clock offset when each beat lands. The muxer then delays each
narration clip to its beat's offset. So whether recall takes 2s or 20s, the
voice still starts talking about the diagnosis exactly when the diagnosis shows.
The only fixed timing is the *minimum dwell* per beat (the TTS clip's own length
+ a small pad), so the visual never cuts away before the sentence finishes.
