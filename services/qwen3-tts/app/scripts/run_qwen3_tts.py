#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _read_request_payload() -> dict:
    req_path = os.environ.get("QWEN3_TTS_REQUEST_JSON", "").strip()
    if not req_path:
        raise RuntimeError("QWEN3_TTS_REQUEST_JSON is not set")
    payload = json.loads(Path(req_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("QWEN3_TTS_REQUEST_JSON must contain a JSON object")
    return payload


def _clamp_speed(speed: object) -> float:
    try:
        value = float(speed)
    except Exception:
        value = 1.0
    return max(0.5, min(2.0, value))


def _voice(payload: dict) -> str:
    raw = str(payload.get("voice") or os.environ.get("QWEN3_TTS_DEFAULT_VOICE") or "en-us").strip()
    return raw or "en-us"


def _text(payload: dict) -> str:
    text = str(payload.get("input") or payload.get("text") or "").strip()
    return text or "silence"


def main() -> int:
    out_path = os.environ.get("QWEN3_TTS_OUTPUT_PATH", "").strip()
    if not out_path:
        raise RuntimeError("QWEN3_TTS_OUTPUT_PATH is not set")

    payload = _read_request_payload()
    text = _text(payload)
    speed = _clamp_speed(payload.get("speed"))
    voice = _voice(payload)
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    words_per_minute = str(int(round(175 * speed)))
    cmd = [
        "espeak-ng",
        "-v",
        voice,
        "-s",
        words_per_minute,
        "-w",
        str(output),
        text,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            "espeak-ng synthesis failed: "
            f"rc={proc.returncode}; stderr={(proc.stderr or '').strip()}; stdout={(proc.stdout or '').strip()}"
        )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("espeak-ng did not produce audio output")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
