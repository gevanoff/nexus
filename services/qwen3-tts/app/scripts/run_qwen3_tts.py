#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    return value.strip()


def _fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _json_env(name: str) -> dict:
    raw = _env(name)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _try_import_qwen3_tts():
    candidates = [
        _env("QWEN3_TTS_APP_DIR"),
        "/var/lib/qwen3-tts/app",
        "/opt/qwen3-tts",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        import soundfile as sf  # type: ignore
        import torch  # type: ignore
        from qwen_tts import Qwen3TTSModel  # type: ignore
    except Exception as exc:
        _fail(
            "Qwen3-TTS local mode dependencies are missing. "
            "Expected module 'qwen_tts' and runtime deps. "
            "Ensure local Qwen3-TTS resources are available (e.g. /var/lib/qwen3-tts/app) and dependencies installed. "
            f"Import error: {type(exc).__name__}: {exc}"
        )
    return Qwen3TTSModel, sf, torch


def _dtype(torch_mod, value: str):
    normalized = value.strip().lower()
    if normalized in {"bf16", "bfloat16"}:
        return torch_mod.bfloat16
    if normalized in {"f16", "float16", "fp16"}:
        return torch_mod.float16
    if normalized in {"f32", "float32", "fp32"}:
        return torch_mod.float32
    return torch_mod.float32


def _read_request_payload() -> dict:
    req_path = _env("QWEN3_TTS_REQUEST_JSON")
    if not req_path:
        _fail("QWEN3_TTS_REQUEST_JSON is not set")
    request_path = Path(req_path)
    if not request_path.exists():
        _fail(f"QWEN3_TTS_REQUEST_JSON does not exist: {request_path}")
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"Failed to parse QWEN3_TTS_REQUEST_JSON: {exc}")
    if not isinstance(payload, dict):
        _fail("QWEN3_TTS_REQUEST_JSON must contain a JSON object")
    return payload


def _text(payload: dict) -> str:
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        _fail("Request JSON missing 'input' text")
    return text


def main() -> int:
    if _bool_env("QWEN3_TTS_READYZ_PROBE", False):
        _read_request_payload()
        _try_import_qwen3_tts()
        return 0

    out_path = _env("QWEN3_TTS_OUTPUT_PATH")
    if not out_path:
        _fail("QWEN3_TTS_OUTPUT_PATH is not set")

    payload = _read_request_payload()
    text = _text(payload)

    Qwen3TTSModel, sf, torch_mod = _try_import_qwen3_tts()

    task = (_env("QWEN3_TTS_TASK") or "custom_voice").strip().lower()
    model_id = _env("QWEN3_TTS_MODEL_ID") or "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    device_map = _env("QWEN3_TTS_DEVICE_MAP") or "cpu"
    dtype = _dtype(torch_mod, _env("QWEN3_TTS_DTYPE") or "float32")
    attn_impl = _env("QWEN3_TTS_ATTN_IMPL")

    model_kwargs = {
        "device_map": device_map,
        "dtype": dtype,
    }
    if attn_impl:
        model_kwargs["attn_implementation"] = attn_impl

    model = Qwen3TTSModel.from_pretrained(model_id, **model_kwargs)

    if task == "voice_design":
        language = _env("QWEN3_TTS_LANGUAGE") or "Auto"
        instruct = _env("QWEN3_TTS_INSTRUCT") or ""
        wavs, sample_rate = model.generate_voice_design(
            text=text,
            language=language,
            instruct=instruct,
        )
    elif task == "voice_clone":
        language = _env("QWEN3_TTS_LANGUAGE") or "Auto"
        ref_audio = _env("QWEN3_TTS_REF_AUDIO")
        ref_text = _env("QWEN3_TTS_REF_TEXT")
        if not ref_audio:
            _fail("QWEN3_TTS_REF_AUDIO is not set for voice_clone")
        x_vector_only = _bool_env("QWEN3_TTS_X_VECTOR_ONLY", False)
        wavs, sample_rate = model.generate_voice_clone(
            text=text,
            language=language,
            ref_audio=ref_audio,
            ref_text=ref_text,
            x_vector_only_mode=x_vector_only,
        )
    else:
        language = _env("QWEN3_TTS_LANGUAGE") or "Auto"
        default_voice_map = {
            "alloy": "Vivian",
            "echo": "Ryan",
            "fable": "Serena",
            "onyx": "Aiden",
            "nova": "Dylan",
            "shimmer": "Ono_Anna",
        }
        voice_map = {**default_voice_map, **_json_env("QWEN3_TTS_VOICE_MAP_JSON")}
        voice = payload.get("voice")
        if isinstance(voice, str) and voice:
            speaker = voice_map.get(voice, voice)
        else:
            speaker = _env("QWEN3_TTS_SPEAKER") or "Vivian"
        instruct = _env("QWEN3_TTS_INSTRUCT") or ""
        wavs, sample_rate = model.generate_custom_voice(
            text=text,
            language=language,
            speaker=speaker,
            instruct=instruct,
        )

    if not wavs:
        _fail("QWEN3_TTS returned empty audio list")

    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output), wavs[0], sample_rate)
    if not output.exists() or output.stat().st_size == 0:
        _fail(f"QWEN3_TTS_OUTPUT_PATH is empty: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
