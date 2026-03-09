import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import logging
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel


logging.getLogger("torchtune.modules.attention").setLevel(logging.ERROR)

app = FastAPI(title="HeartMula", version="0.2")

try:
    from heartlib import HeartMuLaGenPipeline
except ImportError:
    HeartMuLaGenPipeline = None


class MusicGenerationRequest(BaseModel):
    prompt: Optional[str] = None
    lyrics: Optional[str] = None
    style: Optional[str] = None
    duration: Optional[int] = 30
    temperature: Optional[float] = 1.0
    top_k: Optional[int] = 50
    top_p: Optional[float] = None
    tags: Optional[str] = "electronic,ambient"


class MusicGenerationResponse(BaseModel):
    id: str
    status: str
    audio_url: str
    duration: int
    prompt: str


pipeline: Optional[Any] = None
pipeline_device: Optional[str] = None
pipeline_dtype: Optional[str] = None

TAG_PREFIX_RE = re.compile(r"^(genre|style|mood|tags)\s*[:=]\s*", re.IGNORECASE)
STYLE_OF_RE = re.compile(r"\bin the style of\b", re.IGNORECASE)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now() -> int:
    return int(time.time())


def _model_path() -> str:
    return _env("HEARTMULA_MODEL_PATH", "/data/ckpt") or "/data/ckpt"


def _timeout_sec() -> int:
    return _int_env("HEARTMULA_TIMEOUT_SEC", 1200)


def _workdir() -> str:
    return _env("HEARTMULA_WORKDIR", "/data/app") or "/data/app"


def _model_id() -> str:
    return _env("HEARTMULA_MODEL_ID", "HeartMula") or "HeartMula"


def _output_dir() -> Path:
    output_dir = Path(_env("HEARTMULA_OUTPUT_DIR", "/data/output") or "/data/output")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _split_tags(raw: str) -> list[str]:
    return [token.strip().lower() for token in re.split(r"[,\n;/]+", raw) if token.strip()]


def _extract_style_tags(style: str) -> list[str]:
    tags: list[str] = []
    for line in style.splitlines():
        cleaned = TAG_PREFIX_RE.sub("", line.strip())
        cleaned = STYLE_OF_RE.sub("", cleaned).strip()
        if cleaned:
            tags.extend(_split_tags(cleaned))
    return tags


def _merge_tags(*tag_groups: Iterable[str]) -> str:
    seen = set()
    ordered: list[str] = []
    for group in tag_groups:
        for tag in group:
            normalized = tag.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
    return ",".join(ordered)


def _align_tensors_to_device(obj: Any, device: torch.device, target_dtype: Optional[torch.dtype] = None) -> Any:
    if isinstance(obj, torch.Tensor):
        tensor = obj.to(device)
        if target_dtype is not None and tensor.is_floating_point():
            try:
                tensor = tensor.to(target_dtype)
            except Exception:
                pass
        return tensor
    if isinstance(obj, dict):
        return {key: _align_tensors_to_device(value, device, target_dtype) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_align_tensors_to_device(value, device, target_dtype) for value in obj]
    return obj


def _load_pipeline() -> bool:
    global pipeline, pipeline_device, pipeline_dtype
    if HeartMuLaGenPipeline is None:
        return False
    try:
        version = _env("HEARTMULA_VERSION", "3B") or "3B"
        dtype_env = (_env("HEARTMULA_DTYPE", "float32") or "float32").lower()
        requested_device = (_env("HEARTMULA_DEVICE", "") or "").lower()

        if requested_device == "cpu":
            device = torch.device("cpu")
        elif requested_device == "cuda" and torch.cuda.is_available():
            device = torch.device("cuda")
        elif requested_device == "cuda":
            device = torch.device("cpu")
        else:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        if device.type == "cuda" and dtype_env in {"float16", "fp16"}:
            dtype = torch.float16
        else:
            dtype = torch.float32

        pipeline = HeartMuLaGenPipeline.from_pretrained(_model_path(), device=device, dtype=dtype, version=version)
        pipeline_device = str(device)
        pipeline_dtype = str(dtype)
        if (_env("HEARTMULA_LAZY_LOAD", "false") or "false").lower() in {"1", "true", "yes", "on"}:
            pipeline.lazy_load = True
        return True
    except Exception:
        pipeline = None
        pipeline_device = None
        pipeline_dtype = None
        return False


@app.on_event("startup")
async def startup_event() -> None:
    if not _load_pipeline():
        raise RuntimeError("Failed to load HeartMula pipeline on startup")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "heartmula-shim"}


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return health()


@app.get("/readyz")
def readyz() -> JSONResponse:
    if pipeline is not None:
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "HeartMula pipeline is not loaded inside the container.",
        },
    )


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {"object": "list", "data": [{"id": _model_id(), "object": "model", "owned_by": "heartmula"}]}


async def _generate_music_impl(request: MusicGenerationRequest) -> MusicGenerationResponse:
    global pipeline
    if pipeline is None:
        raise HTTPException(status_code=503, detail="HeartMula pipeline not initialized")

    try:
        generation_id = str(uuid.uuid4())
        lyrics = request.lyrics or ""
        base_tags = request.tags or "electronic,ambient"
        style_tags = _extract_style_tags(request.style or "")
        tags = _merge_tags(_split_tags(base_tags), style_tags)

        if request.style and not lyrics:
            lyrics = request.style

        if not lyrics and request.prompt:
            prompt = request.prompt.strip()
            if "\n" in prompt or len(prompt.split()) > 20:
                lyrics = prompt
            else:
                tags = _merge_tags(_split_tags(prompt), _split_tags(tags))

        requested_duration = request.duration or 30
        output_path = _output_dir() / f"{generation_id}.wav"

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        pre_kwargs, forward_kwargs, post_kwargs = pipeline._sanitize_parameters(
            cfg_scale=1.5,
            max_audio_length_ms=requested_duration * 1000,
            temperature=request.temperature,
            topk=request.top_k,
            save_path=str(output_path),
        )

        with Path(output_path.with_suffix(".lyrics.txt")).open("w", encoding="utf-8") as lyrics_file:
            lyrics_file.write(lyrics)
        with Path(output_path.with_suffix(".tags.txt")).open("w", encoding="utf-8") as tags_file:
            tags_file.write(tags)

        lyrics_path = str(output_path.with_suffix(".lyrics.txt"))
        tags_path = str(output_path.with_suffix(".tags.txt"))
        try:
            model_inputs = pipeline.preprocess({"lyrics": lyrics_path, "tags": tags_path}, **pre_kwargs)
        finally:
            for temp_path in (lyrics_path, tags_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        device = torch.device(pipeline_device or ("cuda" if torch.cuda.is_available() else "cpu"))
        target_dtype = None
        if pipeline_dtype and "float16" in pipeline_dtype:
            target_dtype = torch.float16
        elif pipeline_dtype and "float32" in pipeline_dtype:
            target_dtype = torch.float32

        model_inputs = _align_tensors_to_device(model_inputs, device, target_dtype)
        model_outputs = pipeline._forward(model_inputs, **forward_kwargs)
        pipeline.postprocess(model_outputs, save_path=str(output_path), **post_kwargs)

        del model_inputs
        del model_outputs
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if not output_path.exists():
            raise RuntimeError("Generation did not produce output file")

        effective_prompt = request.style or request.lyrics or request.prompt or "instrumental"
        return MusicGenerationResponse(
            id=generation_id,
            status="completed",
            audio_url=f"/audio/{output_path.name}",
            duration=requested_duration,
            prompt=effective_prompt,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Music generation failed: {exc}") from exc


@app.post("/v1/music/generations", response_model=MusicGenerationResponse)
async def generate_music(request: MusicGenerationRequest) -> MusicGenerationResponse:
    return await _generate_music_impl(request)


@app.post("/v1/audio/generations", response_model=MusicGenerationResponse)
async def generate_audio(request: MusicGenerationRequest) -> MusicGenerationResponse:
    return await _generate_music_impl(request)


@app.get("/audio/{filename}")
async def get_audio(filename: str) -> FileResponse:
    file_path = _output_dir() / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(path=file_path, media_type="audio/wav", filename=filename)