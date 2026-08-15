"""Text-to-speech for the narration — one audio clip per scene.

Pluggable engine (``--engine``):
  * ``gemini`` (default) — Google's ``gemini-2.5-flash-preview-tts`` neural
    voices. Reuses the SAME ``GEMINI_API_KEY`` felix already needs, so there's
    no new account or key. Sounds like a real narrator, not a robot. Returns
    raw 24kHz PCM which we wrap to WAV.
  * ``say`` — macOS ``say`` (offline, no key). The robotic fallback; use it when
    there's no network / key, or to iterate on pacing without spending quota.

Either way, ffmpeg normalizes each clip to 44.1k mono WAV so they concatenate
cleanly, and ffprobe measures the exact duration. Artifacts in --out-dir:
  * ``<key>.wav``  — the spoken clip
  * ``dwell.json`` — {scene_key: clip_seconds + PAD} — handed to the driver so
    it lingers on each beat at least as long as the voice speaks over it.

List Gemini voices: see GEMINI_VOICES below. List `say` voices: ``say -v '?'``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import wave
from pathlib import Path

from .script import SCENES

# ── engine config ────────────────────────────────────────────────────────────

# A calm, authoritative Gemini narrator voice. Others: Kore, Puck, Aoede, Fenrir,
# Leda, Orus, Zephyr — swap via --voice.
GEMINI_VOICE = "Charon"
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_PCM_RATE = 24_000  # Gemini TTS returns 24kHz 16-bit mono PCM

# macOS `say` fallback.
SAY_VOICE = "Samantha"
SAY_RATE_WPM = 180

# Padding added to each clip's measured length before it becomes the driver's
# min-dwell, so the visual never cuts before the sentence finishes + a beat.
DWELL_PAD_S = 1.2


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _normalize_to_wav(src: Path, dst: Path) -> None:
    """Transcode any input to 44.1k mono WAV so all clips mix uniformly."""
    _run(["ffmpeg", "-y", "-i", str(src), "-ar", "44100", "-ac", "1", str(dst)])


# ── Gemini engine ────────────────────────────────────────────────────────────


class _GeminiTTS:
    """Lazily-constructed Gemini TTS client (mirrors clients/llm/gemini.py's
    lazy-import so importing this module needs no SDK / key / network)."""

    def __init__(self, voice: str):
        self.voice = voice
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            from src.config import get_settings

            key = get_settings().gemini_api_key
            if not key:
                raise RuntimeError("GEMINI_API_KEY is not set — use --engine say for offline TTS")
            self._client = genai.Client(api_key=key)
        return self._client

    def synth(self, text: str, out_dir: Path, key: str) -> Path:
        """Synthesize `text` to `<key>.wav` (via a raw-PCM intermediate)."""
        import time

        from google.genai import types
        from google.genai.errors import ClientError

        client = self._get_client()
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                )
            ),
        )
        # Ride through transient per-minute rate limits (429). The free tier also
        # has a hard PER-DAY cap (10 requests) that no backoff can clear — that
        # surfaces as a 429 too; we retry a couple of times then re-raise so the
        # caller sees it rather than looping forever.
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=GEMINI_TTS_MODEL, contents=text, config=cfg
                )
                break
            except ClientError as e:
                if e.code == 429 and attempt < 2:
                    print(f"[audio:gemini] 429 on {key}; backing off 40s (attempt {attempt+1}/3)")
                    time.sleep(40)
                    continue
                raise
        pcm = resp.candidates[0].content.parts[0].inline_data.data
        raw_wav = out_dir / f"{key}.raw.wav"
        with wave.open(str(raw_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)  # 16-bit
            w.setframerate(GEMINI_PCM_RATE)
            w.writeframes(pcm)
        wav = out_dir / f"{key}.wav"
        _normalize_to_wav(raw_wav, wav)
        raw_wav.unlink(missing_ok=True)
        return wav


# ── say engine ───────────────────────────────────────────────────────────────


def _say_synth(text: str, out_dir: Path, key: str, voice: str) -> Path:
    aiff = out_dir / f"{key}.aiff"
    wav = out_dir / f"{key}.wav"
    _run(["say", "-v", voice, "-r", str(SAY_RATE_WPM), "-o", str(aiff), text])
    _normalize_to_wav(aiff, wav)
    aiff.unlink(missing_ok=True)
    return wav


# ── build ────────────────────────────────────────────────────────────────────


def build(out_dir: Path, engine: str = "gemini", voice: str | None = None) -> dict[str, float]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if engine == "gemini":
        tts = _GeminiTTS(voice or GEMINI_VOICE)
        synth = lambda text, key: tts.synth(text, out_dir, key)  # noqa: E731
    elif engine == "say":
        v = voice or SAY_VOICE
        synth = lambda text, key: _say_synth(text, out_dir, key, v)  # noqa: E731
    else:
        raise ValueError(f"unknown --engine {engine!r} (use 'gemini' or 'say')")

    dwell: dict[str, float] = {}
    for s in SCENES:
        wav = out_dir / f"{s.key}.wav"
        # Incremental: reuse a clip already synthesized (a re-run after a partial
        # failure — e.g. a mid-batch 429 — doesn't re-spend quota on done scenes).
        if wav.is_file():
            print(f"[audio:{engine}] {s.key:18} (cached)")
        else:
            wav = synth(s.narration, s.key)
        dur = _ffprobe_duration(wav)
        dwell[s.key] = round(dur + DWELL_PAD_S, 2)
        print(f"[audio:{engine}] {s.key:18} {dur:5.2f}s  -> dwell {dwell[s.key]:.2f}s")
    (out_dir / "dwell.json").write_text(json.dumps(dwell, indent=2))
    print(f"[audio] wrote {out_dir / 'dwell.json'}")
    return dwell


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesize the demo narration (TTS).")
    ap.add_argument("--out-dir", default="demo/out/audio", help="where clips + dwell.json land")
    ap.add_argument("--engine", default="gemini", choices=["gemini", "say"], help="TTS backend")
    ap.add_argument("--voice", default=None, help="voice name (engine-specific; defaults per engine)")
    args = ap.parse_args()
    build(Path(args.out_dir), engine=args.engine, voice=args.voice)


if __name__ == "__main__":
    main()
