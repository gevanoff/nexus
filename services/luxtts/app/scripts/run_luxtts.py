#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import struct
import wave
from pathlib import Path


def _read_request_text() -> str:
    req_path = os.environ.get("LUXTTS_REQUEST_JSON", "").strip()
    if not req_path:
        raise RuntimeError("LUXTTS_REQUEST_JSON is not set")
    payload = json.loads(Path(req_path).read_text(encoding="utf-8"))
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        text = "silence"
    return text


def _synthesize_pcm16(text: str, sample_rate: int = 24000) -> bytes:
    # Lightweight, dependency-free fallback synthesizer.
    # Produces a short voiced-like tone sequence per character.
    frames: list[int] = []
    base = 170.0
    char_duration = 0.05
    gap_duration = 0.01
    amp = 0.20

    for idx, ch in enumerate(text[:500]):
        freq = base + ((ord(ch) + (idx * 7)) % 220)
        samples = max(1, int(sample_rate * char_duration))
        for i in range(samples):
            t = i / sample_rate
            envelope = min(1.0, i / max(1.0, samples * 0.1)) * min(1.0, (samples - i) / max(1.0, samples * 0.15))
            s = amp * envelope * math.sin(2.0 * math.pi * freq * t)
            frames.append(max(-32767, min(32767, int(s * 32767.0))))

        gap = max(1, int(sample_rate * gap_duration))
        frames.extend([0] * gap)

    if not frames:
        frames = [0] * int(sample_rate * 0.25)

    return struct.pack("<" + "h" * len(frames), *frames)


def main() -> int:
    out_path = os.environ.get("LUXTTS_OUTPUT_PATH", "").strip()
    if not out_path:
        raise RuntimeError("LUXTTS_OUTPUT_PATH is not set")

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
