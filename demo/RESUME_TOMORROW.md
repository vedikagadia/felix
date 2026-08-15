# Resume tomorrow — finishing the demo video

The pipeline is complete and wired; it stalled only on the **Gemini TTS free-tier
cap of 10 requests/day** (429 RESOURCE_EXHAUSTED). The quota resets ~24h after
you first hit it (2026-08-14). One command tomorrow finishes everything.

## Just run this (from repo root, with the venv + local CockroachDB up + built frontend)

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # ffmpeg lives here
PYTHONPATH=. ./.venv/bin/python -m demo.orchestrate --port 8021 --interval 0.3
# → demo/out/felix-demo.mp4
```

- Do **NOT** pass `--skip-audio` — you want it to synthesize the 6 remaining
  clips. It will NOT re-spend quota on the 4 already cached (synth is
  incremental: an existing `demo/out/audio/<key>.wav` is reused).
- That's **6 new TTS calls** — safely under the 10/day cap, with margin.

## State as of 2026-08-14

- **Narration:** rewritten + deep (see `script.py`). Explains the sample-project
  setup, proves BOTH RCAs with real numbers (pool_size=10 held for seconds via
  0.5s→256s backoff; p99≈2000ms vs avg≈258ms over 60 samples), and has a
  dedicated beat on the real-time CDC alert (setup, trip rule, diagnosis).
- **Engine:** Gemini TTS (`gemini-2.5-flash-preview-tts`, voice `Charon`) —
  natural, not robotic. Reuses `GEMINI_API_KEY`. `--engine say` is the offline
  fallback (robotic). `--voice Kore|Puck|Aoede|…` to try other voices.
- **Cached & verified-current clips (4):** intro, puzzle_b_diagnosis, cdc_setup,
  cdc_alert. Durations match the new script, so they're NOT stale.
- **To synthesize tomorrow (6):** setup, puzzle_a_submit, puzzle_a_diagnosis,
  puzzle_a_graph, puzzle_b_submit, cdc_diagnosis.

## If you edit the narration first

Editing a scene's text does NOT auto-invalidate its cached clip. Delete the stale
clip so it re-synthesizes (costs 1 quota each):
```bash
rm demo/out/audio/<scene_key>.wav
```

## If you run out of quota again

Options: (a) enable Gemini API billing (removes the cap, costs well under $1);
(b) `--engine say` for a free robotic build to check timing; (c) wait another day.
The full run was verified working end-to-end on 2026-08-13 (all 8 UI beats incl.
both live-CDC beats) — only the voice track is pending.
```
