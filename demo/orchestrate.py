"""One command → the finished demo video.

    python -m demo.orchestrate

Pipeline:
  1. TTS   — synthesize per-scene narration clips + a dwell map (demo/audio.py).
  2. Stack — start the live backend the recording plays against:
               * `python -m src serve`  (API + built frontend, one port)
               * `python -m src watch`  (CDC watcher — trips on the p99 spike)
               * `python -m sample_project.run`  (emitter — produces the spike)
             We reset the transient CDC state first so the watcher re-arms and a
             fresh alert fires during THIS recording.
  3. Drive — record the browser walking the story, gated on real UI state, and
             capture the wall-clock timestamp of each beat (demo/driver.py).
  4. Mux   — lay narration clip N onto the recorded video at beat N's timestamp
             (ffmpeg), producing demo/out/felix-demo.mp4.

Everything is torn down in reverse on exit, even on Ctrl-C or failure. Requires:
a seeded local CockroachDB, GEMINI_API_KEY (the watcher must reason on a trip),
plus `ffmpeg` and `playwright` (with `playwright install chromium`) — see
demo/README.md, which the orchestrator points you at if a prereq is missing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── prereq checks (fail fast, with the fix) ─────────────────────────────────


def _preflight(url_port: int) -> None:
    missing: list[str] = []
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        missing.append("ffmpeg (brew install ffmpeg)")
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        missing.append("playwright (pip install playwright && playwright install chromium)")
    try:
        from src.config import get_settings

        s = get_settings()
        if s.llm_provider == "gemini" and not s.gemini_api_key:
            missing.append("GEMINI_API_KEY (the watcher must reason on a CDC trip) — set it in .env")
    except Exception as e:  # noqa: BLE001
        missing.append(f"could not read settings: {e}")
    if missing:
        print("demo: missing prerequisites:\n  - " + "\n  - ".join(missing))
        print("See demo/README.md for the full setup.")
        sys.exit(1)


# ── the live stack (subprocesses) ────────────────────────────────────────────


class Stack:
    """Starts + supervises serve / watch / emitter, tears them all down on exit."""

    def __init__(self, host: str, port: int, interval: float):
        self.host = host
        self.port = port
        self.interval = interval
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def _spawn(self, name: str, args: list[str]) -> None:
        log = open(REPO_ROOT / "demo" / "out" / f"{name}.log", "w")
        p = subprocess.Popen(
            [sys.executable, "-m", *args],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**_env()},
        )
        self.procs.append((name, p))
        print(f"[stack] started {name} (pid {p.pid}) -> demo/out/{name}.log")

    def start(self) -> None:
        self._reset_cdc_state()
        self._spawn("serve", ["src", "serve", "--host", self.host, "--port", str(self.port)])
        self._wait_for_http()
        # Watcher + emitter feed the live CDC alert the driver waits for.
        self._spawn("watch", ["src", "watch", "--debug"])
        self._spawn("emitter", ["sample_project.run", "--interval", str(self.interval)])

    def _reset_cdc_state(self) -> None:
        """Resolve any open cdc session + clear the metrics table so the watcher
        re-arms and a NEW alert fires during this run (see SETUP.md §7)."""
        try:
            from src.store.connection import get_conn

            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE active_incidents SET status='resolved' WHERE source='cdc'")
                cur.execute("TRUNCATE metrics")
            conn.commit()
            conn.close()
            print("[stack] reset CDC state (resolved open cdc sessions, truncated metrics)")
        except Exception as e:  # noqa: BLE001 — non-fatal; the dedup guard still holds
            print(f"[stack] warning: could not reset CDC state ({e}); a stale alert may show instead")

    def _wait_for_http(self, timeout_s: float = 30.0) -> None:
        import urllib.request

        deadline = time.monotonic() + timeout_s
        health = f"http://{self.host}:{self.port}/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health, timeout=2) as r:
                    if r.status == 200:
                        print("[stack] serve is up")
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        raise RuntimeError(f"serve did not become healthy at {health} within {timeout_s:.0f}s")

    def stop(self) -> None:
        for name, p in reversed(self.procs):
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        # Give them a moment to exit cleanly (the watcher finishes its changefeed
        # job on SIGINT), then hard-kill any stragglers.
        time.sleep(2)
        for name, p in reversed(self.procs):
            if p.poll() is None:
                p.kill()
            print(f"[stack] stopped {name}")


def _env() -> dict:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


# ── mux: narration onto the recorded timeline ────────────────────────────────


def mux(out_dir: Path, audio_dir: Path) -> Path:
    """Overlay each scene's TTS clip onto demo.webm at that beat's timestamp,
    producing felix-demo.mp4. Uses ffmpeg's adelay (per-clip offset) + amix."""
    from .script import SCENES

    beats = json.loads((out_dir / "beats.json").read_text())
    video = out_dir / "demo.webm"
    out = out_dir / "felix-demo.mp4"

    # Build one delayed audio input per scene that actually got recorded.
    inputs: list[str] = ["-i", str(video)]
    filters: list[str] = []
    mixed_labels: list[str] = []
    audio_idx = 1  # input 0 is the video
    for s in SCENES:
        if s.key not in beats:
            continue  # e.g. CDC beats skipped when the watcher didn't trip
        clip = audio_dir / f"{s.key}.wav"
        if not clip.is_file():
            continue
        delay_ms = int(float(beats[s.key]) * 1000)
        inputs += ["-i", str(clip)]
        # adelay wants a value per channel; the clips are mono.
        filters.append(f"[{audio_idx}:a]adelay={delay_ms}|{delay_ms}[a{audio_idx}]")
        mixed_labels.append(f"[a{audio_idx}]")
        audio_idx += 1

    if not mixed_labels:
        raise RuntimeError("no narration clips matched recorded beats — nothing to mux")

    filters.append(
        f"{''.join(mixed_labels)}amix=inputs={len(mixed_labels)}:normalize=0[aout]"
    )
    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        # Re-encode video to H.264/mp4 for universal playback; AAC audio.
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(out),
    ]
    # No -shortest: the video is the master track. The driver lingers
    # (clip_length + pad) after each beat, so the video always outlasts the last
    # narration clip — the output should run to the video's end, not the audio's.
    print("[mux] running ffmpeg…")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    print(f"[mux] wrote {out}")
    return out


# ── orchestration ─────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Produce the felix demo video end to end.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--out-dir", default="demo/out")
    ap.add_argument("--engine", default="gemini", choices=["gemini", "say"], help="TTS backend")
    ap.add_argument("--voice", default=None, help="voice name (engine-specific; defaults per engine)")
    ap.add_argument("--interval", type=float, default=0.4, help="emitter tick seconds (faster = trips sooner)")
    ap.add_argument("--cdc-timeout", type=float, default=120.0, help="max wait for the CDC alert")
    ap.add_argument("--skip-audio", action="store_true", help="reuse existing TTS clips")
    args = ap.parse_args()

    out_dir = (REPO_ROOT / args.out_dir).resolve()
    audio_dir = out_dir / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    _preflight(args.port)

    # 1. TTS (also yields the dwell map the driver paces off of).
    from .audio import build as build_audio

    if args.skip_audio and (audio_dir / "dwell.json").is_file():
        print("[audio] --skip-audio: reusing existing clips")
    else:
        build_audio(audio_dir, engine=args.engine, voice=args.voice)

    # 2–3. Stack up, record, tear down (always).
    stack = Stack(args.host, args.port, args.interval)
    try:
        stack.start()
        from .driver import run as drive

        dwell = json.loads((audio_dir / "dwell.json").read_text())
        drive(
            url=f"http://{args.host}:{args.port}",
            out_dir=out_dir,
            dwell=dwell,
            cdc_timeout_s=args.cdc_timeout,
        )
    finally:
        stack.stop()

    # 4. Mux.
    final = mux(out_dir, audio_dir)
    print(f"\n✅ demo video ready: {final}")


if __name__ == "__main__":
    main()
