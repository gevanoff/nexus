#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    return value.strip()


def _fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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


def _refs_dir() -> str:
    return _env("LUXTTS_REFS_DIR") or "/var/lib/luxtts/voices"


def _discover_ref_map() -> dict:
    directory = _refs_dir()
    if not directory:
        return {}
    refs = Path(directory)
    if not refs.exists() or not refs.is_dir():
        return {}
    allowed_exts = {".wav", ".mp3", ".ogg", ".webm", ".flac", ".m4a"}
    out = {}
    for entry in refs.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in allowed_exts:
            continue
        key = entry.stem.strip()
        if not key:
            continue
        out[key] = str(entry)
        out[key.lower()] = str(entry)
    return out


def _try_import_luxtts():
    candidates = [
        _env("LUXTTS_APP_DIR"),
        "/var/lib/luxtts/app",
        "/opt/luxtts",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        from zipvoice.luxvoice import LuxTTS  # type: ignore
        import soundfile as sf  # type: ignore
        import torch  # type: ignore
    except Exception as exc:
        _fail(
            "LuxTTS local mode dependencies are missing. "
            "Expected module 'zipvoice.luxvoice' and runtime deps. "
            "Ensure local LuxTTS resources are available (e.g. /var/lib/luxtts/app) and dependencies installed. "
            f"Import error: {type(exc).__name__}: {exc}"
        )
    return LuxTTS, sf, torch


def _device(torch_mod) -> str:
    value = _env("LUXTTS_DEVICE") or "auto"
    if value != "auto":
        return value
    if torch_mod.cuda.is_available():
        return "cuda"
    backend_mps = getattr(torch_mod.backends, "mps", None)
    if backend_mps is not None and backend_mps.is_available():
        return "mps"
    return "cpu"


def _read_request_payload() -> dict:
    req_path = _env("LUXTTS_REQUEST_JSON")
    if not req_path:
        _fail("LUXTTS_REQUEST_JSON is not set")
    request_path = Path(req_path)
    if not request_path.exists():
        _fail(f"LUXTTS_REQUEST_JSON does not exist: {request_path}")
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"Failed to parse LUXTTS_REQUEST_JSON: {exc}")
    if not isinstance(payload, dict):
        _fail("LUXTTS_REQUEST_JSON must contain a JSON object")
    return payload


def _text(payload: dict) -> str:
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        _fail("Request JSON missing 'input' text")
    return text


def _payload_float(payload: dict, key: str) -> float | None:
    if key not in payload:
        return None
    raw = payload.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _resolve_prompt_audio(payload: dict) -> str:
    voice = payload.get("voice") or _env("LUXTTS_VOICE")
    voice_map = {**_discover_ref_map(), **_json_env("LUXTTS_VOICE_MAP_JSON")}
    mapped_prompt = None
    if isinstance(voice, str):
        mapped_prompt = voice_map.get(voice) or voice_map.get(voice.lower())
    explicit_prompt = _env("LUXTTS_PROMPT_AUDIO") or payload.get("prompt_audio")
    default_prompts = [
        str(Path(_refs_dir()) / "penny.wav"),
        str(Path(_refs_dir()) / "prompt.wav"),
        "/var/lib/luxtts/penny.wav",
        "/var/lib/luxtts/prompt.wav",
    ]

    candidates = []
    if mapped_prompt:
        candidates.append(str(mapped_prompt).strip())
    if explicit_prompt:
        candidates.append(str(explicit_prompt).strip())
    candidates.extend(default_prompts)

    seen = set()
    normalized = []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)

    for candidate in normalized:
        prompt_path = Path(candidate)
        if prompt_path.exists() and prompt_path.is_file():
            return str(prompt_path)

    _fail(
        "Configured LuxTTS prompt audio file does not exist or is not a file. "
        f"Checked: {', '.join(normalized)}. Ensure one exists inside container "
        "(e.g. /var/lib/luxtts/voices/penny.wav or /var/lib/luxtts/voices/prompt.wav)."
    )


def _fallback_prompt_audio() -> str:
    candidates = [
        str(Path(_refs_dir()) / "penny.wav"),
        str(Path(_refs_dir()) / "prompt.wav"),
        "/var/lib/luxtts/penny.wav",
        "/var/lib/luxtts/prompt.wav",
    ]
    for candidate in candidates:
        p = Path(candidate)
        if p.exists() and p.is_file():
            return str(p)
    return ""


def _pad_prompt_audio(prompt_audio: str, sf, *, min_duration_sec: float = 2.0) -> str:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return ""

    try:
        samples, sample_rate = sf.read(prompt_audio, always_2d=False)
    except Exception:
        return ""

    try:
        target_samples = max(int(float(sample_rate) * float(min_duration_sec)), 1)
    except Exception:
        target_samples = 1

    try:
        current_samples = int(samples.shape[0])
    except Exception:
        return ""

    if current_samples >= target_samples:
        return str(prompt_audio)

    pad_samples = target_samples - current_samples
    try:
        if getattr(samples, "ndim", 1) == 1:
            padded = np.pad(samples, (0, pad_samples), mode="constant")
        else:
            padded = np.pad(samples, ((0, pad_samples), (0, 0)), mode="constant")
    except Exception:
        return ""

    try:
        handle = tempfile.NamedTemporaryFile(prefix="luxtts-prompt-", suffix=".wav", delete=False)
        handle.close()
        sf.write(handle.name, padded, sample_rate)
        return str(handle.name)
    except Exception:
        return ""


def _is_lux_kernel_size_error(exc: Exception) -> bool:
    message = str(exc or "")
    return "Kernel size can't be greater than actual input size" in message


def main() -> int:
    if _bool_env("LUXTTS_READYZ_PROBE", False):
        payload = _read_request_payload()
        _resolve_prompt_audio(payload)
        _try_import_luxtts()
        return 0

    out_path = _env("LUXTTS_OUTPUT_PATH")
    if not out_path:
        _fail("LUXTTS_OUTPUT_PATH is not set")

    payload = _read_request_payload()
    text = _text(payload)
    prompt_audio = _resolve_prompt_audio(payload)

    LuxTTS, sf, torch_mod = _try_import_luxtts()

    model_id = _env("LUXTTS_MODEL_ID") or "YatharthS/LuxTTS"
    device = _device(torch_mod)
    threads = _int_env("LUXTTS_CPU_THREADS", 0)
    if device == "cpu" and threads > 0:
        lux_tts = LuxTTS(model_id, device=device, threads=threads)
    else:
        lux_tts = LuxTTS(model_id, device=device)

    rms = _float_env("LUXTTS_RMS", 0.01)
    ref_duration = _int_env("LUXTTS_REF_DURATION", 0)
    if ref_duration > 0:
        encoded_prompt = lux_tts.encode_prompt(prompt_audio, duration=ref_duration, rms=rms)
    else:
        encoded_prompt = lux_tts.encode_prompt(prompt_audio, rms=rms)

    num_steps = _int_env("LUXTTS_NUM_STEPS", 4)
    t_shift = _float_env("LUXTTS_T_SHIFT", 0.9)
    requested_speed = _payload_float(payload, "speed")
    if requested_speed is None:
        speed = _float_env("LUXTTS_SPEED", 1.0)
    else:
        speed = max(0.5, min(2.0, requested_speed))
    return_smooth = _bool_env("LUXTTS_RETURN_SMOOTH", False)

    padded_prompt_path = ""
    try:
        final_wav = lux_tts.generate_speech(
            text,
            encoded_prompt,
            num_steps=num_steps,
            t_shift=t_shift,
            speed=speed,
            return_smooth=return_smooth,
        )
    except Exception as exc:
        if not _is_lux_kernel_size_error(exc):
            raise
        padded_prompt_path = _pad_prompt_audio(prompt_audio, sf, min_duration_sec=2.0)
        if not padded_prompt_path or padded_prompt_path == prompt_audio:
            _fail(
                "Reference audio is too short for LuxTTS prompt encoding. "
                "Use a longer sample or record at least a couple of seconds of speech."
            )
        if ref_duration > 0:
            encoded_prompt = lux_tts.encode_prompt(padded_prompt_path, duration=ref_duration, rms=rms)
        else:
            encoded_prompt = lux_tts.encode_prompt(padded_prompt_path, rms=rms)
        final_wav = lux_tts.generate_speech(
            text,
            encoded_prompt,
            num_steps=num_steps,
            t_shift=t_shift,
            speed=speed,
            return_smooth=return_smooth,
        )

    try:
        final_wav = final_wav.numpy().squeeze()
    except Exception:
        pass

    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output), final_wav, 48000)
    if not output.exists() or output.stat().st_size == 0:
        _fail(f"LUXTTS_OUTPUT_PATH is empty: {output}")

    if padded_prompt_path:
        try:
            Path(padded_prompt_path).unlink(missing_ok=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
