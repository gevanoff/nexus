from __future__ import annotations

import io
import os
import random
import re
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="Nexus Chatterbox Turbo TTS", version="0.1")

_MODEL = None
_MODEL_LOCK = threading.Lock()
_SYNTH_LOCK = threading.Lock()

_AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".webm", ".flac", ".m4a", ".aac"}
_VOICE_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return (value if value is not None else default).strip()


def _refs_dir() -> Path:
    return Path(_env("CHATTERBOX_TTS_REFS_DIR", "/var/lib/tts_refs"))


def _device() -> str:
    configured = _env("CHATTERBOX_TTS_DEVICE", "auto").lower()
    if configured and configured != "auto":
        return configured
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            _MODEL = ChatterboxTurboTTS.from_pretrained(device=_device())
    return _MODEL


def _discover_voices() -> list[str]:
    out = ["default"]
    root = _refs_dir()
    if not root.is_dir():
        return out
    seen = {"default"}
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
            continue
        voice = path.stem.strip()
        key = voice.lower()
        if voice and key not in seen:
            seen.add(key)
            out.append(voice)
    return out


def _resolve_voice(voice: Optional[str]) -> Optional[str]:
    raw = (voice or "default").strip()
    if not raw or raw.lower() == "default":
        return None
    if not _VOICE_STEM_RE.fullmatch(raw) or raw in {".", ".."}:
        raise HTTPException(status_code=400, detail="voice must be a reference-library filename stem")

    root = _refs_dir().expanduser().resolve()
    for ext in sorted(_AUDIO_EXTS):
        candidate = (root / f"{raw}{ext}").resolve()
        if candidate != root and root not in candidate.parents:
            continue
        if candidate.is_file():
            return str(candidate)
    raise HTTPException(status_code=400, detail=f"unknown Chatterbox voice/reference: {raw}")

class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input: Optional[str] = None
    text: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = "default"
    response_format: str = Field(default="wav", pattern=r"^wav$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    temperature: float = Field(default=0.8, gt=0.0, le=2.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    top_k: int = Field(default=1000, ge=1, le=5000)
    repetition_penalty: float = Field(default=1.2, ge=1.0, le=3.0)
    seed: Optional[int] = Field(default=None, ge=0, le=2_147_483_647)
    norm_loudness: bool = True


def _time_stretch(wav: np.ndarray, speed: float) -> np.ndarray:
    if abs(speed - 1.0) < 1e-6:
        return wav
    import librosa
    return librosa.effects.time_stretch(wav.astype(np.float32, copy=False), rate=float(speed))


def _synthesize(req: SpeechRequest) -> tuple[bytes, int]:
    text = (req.input or req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input or text is required")

    prompt = _resolve_voice(req.voice)
    model = _model()

    kwargs: dict[str, Any] = {
        "temperature": req.temperature,
        "top_p": req.top_p,
        "top_k": req.top_k,
        "repetition_penalty": req.repetition_penalty,
        "norm_loudness": req.norm_loudness,
    }
    if prompt:
        kwargs["audio_prompt_path"] = prompt

    # ChatterboxTurboTTS mutates cached conditionals when a reference voice is
    # prepared, so serialize generation until the upstream model exposes a
    # request-local conditioning API.
    with _SYNTH_LOCK, torch.inference_mode():
        if req.seed is not None:
            seed = int(req.seed)
            random.seed(seed)
            np.random.seed(seed % (2**32 - 1))
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        wav = model.generate(text, **kwargs)

    arr = wav.squeeze().detach().cpu().numpy().astype(np.float32, copy=False)
    arr = _time_stretch(arr, req.speed)
    buffer = io.BytesIO()
    sf.write(buffer, arr, int(model.sr), format="WAV", subtype="PCM_16")
    return buffer.getvalue(), int(model.sr)


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "chatterbox-tts", "model": "chatterbox-turbo"}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    try:
        import chatterbox.tts_turbo  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"chatterbox import failed: {exc}") from exc
    return {"ok": True, "device": _device(), "model_loaded": _MODEL is not None}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "chatterbox-turbo", "object": "model", "owned_by": "resemble-ai"}],
    }


@app.get("/v1/voices")
@app.get("/voices")
def voices() -> list[str]:
    return _discover_voices()


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "name": "chatterbox-tts",
        "version": "0.1",
        "model": "chatterbox-turbo",
        "device": _device(),
        "voice_cloning": True,
        "paralinguistic_tags": True,
        "reference_min_seconds": 5,
        "controls": {
            "speed": {"type": "number", "min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0},
            "temperature": {"type": "number", "min": 0.1, "max": 2.0, "step": 0.05, "default": 0.8},
            "top_p": {"type": "number", "min": 0.05, "max": 1.0, "step": 0.05, "default": 0.95},
            "top_k": {"type": "integer", "min": 1, "max": 5000, "step": 1, "default": 1000},
            "repetition_penalty": {"type": "number", "min": 1.0, "max": 3.0, "step": 0.05, "default": 1.2},
            "seed": {"type": "integer", "min": 0, "max": 2147483647, "nullable": True},
            "norm_loudness": {"type": "boolean", "default": True},
        },
        "notes": [
            "Use a >5 second reference clip from the shared TTS voice library for zero-shot cloning.",
            "Turbo supports inline paralinguistic tags such as [laugh], [chuckle], and [cough].",
            "Turbo ignores original Chatterbox CFG/exaggeration/min_p controls; Nexus does not expose them.",
            "Speed is applied as pitch-preserving post-generation time stretching.",
        ],
    }


@app.post("/v1/audio/speech")
def speech(req: SpeechRequest) -> Response:
    audio, sample_rate = _synthesize(req)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Model": "chatterbox-turbo",
            "X-TTS-Voice": (req.voice or "default"),
            "X-TTS-Sample-Rate": str(sample_rate),
        },
    )
