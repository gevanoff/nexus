#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import struct
import wave
from pathlib import Path


def _read_request_text() -> str:
    req_path = os.environ.get("QWEN3_TTS_REQUEST_JSON", "").strip()
    if not req_path:
        raise RuntimeError("QWEN3_TTS_REQUEST_JSON is not set")
    payload = json.loads(Path(req_path).read_text(encoding="utf-8"))
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        text = "silence"
    return text


def _synthesize_pcm16(text: str, sample_rate: int = 24000) -> bytes:
    # Lightweight, dependency-free fallback synthesizer.
    frames: list[int] = []
    base = 140.0
    char_duration = 0.045
    gap_duration = 0.008
    amp = 0.22

    for idx, ch in enumerate(text[:500]):
        freq = base + ((ord(ch) * 3 + (idx * 11)) % 260)
        samples = max(1, int(sample_rate * char_duration))
        for i in range(samples):
            t = i / sample_rate
            harmonic = 0.65 * math.sin(2.0 * math.pi * freq * t) + 0.35 * math.sin(2.0 * math.pi * (freq * 1.6) * t)
            envelope = min(1.0, i / max(1.0, samples * 0.1)) * min(1.0, (samples - i) / max(1.0, samples * 0.2))
            s = amp * envelope * harmonic
            frames.append(max(-32767, min(32767, int(s * 32767.0))))

        gap = max(1, int(sample_rate * gap_duration))
        frames.extend([0] * gap)

    if not frames:
        frames = [0] * int(sample_rate * 0.25)

    return struct.pack("<" + "h" * len(frames), *frames)


def main() -> int:
    out_path = os.environ.get("QWEN3_TTS_OUTPUT_PATH", "").strip()
    if not out_path:
        raise RuntimeError("QWEN3_TTS_OUTPUT_PATH is not set")

    text = _read_request_text()
    pcm = _synthesize_pcm16(text)
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
