from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _download_input(value: str, suffix: str = ".png") -> str:
    if not value.startswith(("http://", "https://")):
        return value
    tmpdir = Path(tempfile.mkdtemp(prefix="nexus-media-input-"))
    destination = tmpdir / f"input{suffix}"
    with urllib.request.urlopen(value, timeout=120) as response:
        destination.write_bytes(response.read())
    return str(destination)


def _snap_ltx_frames(frames: int) -> int:
    # LTX latent temporal dimensions require 8k+1 frames.
    frames = max(9, min(frames, 257))
    return ((frames - 1) // 8) * 8 + 1


def _ltx_dimensions(payload: dict[str, Any]) -> tuple[int, int]:
    width = _as_int(payload.get("width"), 0)
    height = _as_int(payload.get("height"), 0)
    if width > 0 and height > 0:
        return width - width % 32, height - height % 32
    resolution = str(payload.get("resolution") or "720p").strip().lower()
    if resolution in {"480p", "480", "540p", "540"}:
        return 704, 416
    return 768, 512


def _build_ltx(payload: dict[str, Any], output_file: Path) -> list[str]:
    width, height = _ltx_dimensions(payload)
    fps = max(1, _as_int(payload.get("fps"), 24))
    requested_frames = _as_int(payload.get("num_frames"), 0)
    if requested_frames <= 0:
        duration = max(1, _as_int(payload.get("duration_seconds") or payload.get("duration"), 5))
        requested_frames = duration * fps + 1
    frames = _snap_ltx_frames(requested_frames)

    args = [
        _env("MEDIA_RUNNER_PYTHON", sys.executable),
        "-m",
        "ltx_pipelines.distilled",
        "--distilled-checkpoint-path",
        _env("LTX_DISTILLED_CHECKPOINT_PATH"),
        "--spatial-upsampler-path",
        _env("LTX_SPATIAL_UPSAMPLER_PATH"),
        "--gemma-root",
        _env("LTX_GEMMA_ROOT"),
        "--prompt",
        str(payload.get("prompt") or ""),
        "--output-path",
        str(output_file),
        "--height",
        str(height),
        "--width",
        str(width),
        "--num-frames",
        str(frames),
        "--frame-rate",
        str(fps),
        "--seed",
        str(_as_int(payload.get("seed"), 42)),
        "--quantization",
        str(payload.get("quantization") or _env("LTX_QUANTIZATION", "fp8-cast")),
    ]
    return args


def _build_hunyuan(payload: dict[str, Any], output_file: Path) -> list[str]:
    upstream = Path(_env("MEDIA_UPSTREAM_DIR", "/data/app"))
    script = upstream / "generate.py"
    resolution = str(payload.get("resolution") or "720p").strip().lower()
    if resolution not in {"480p", "720p"}:
        resolution = "720p"
    duration = max(1, _as_int(payload.get("duration_seconds") or payload.get("duration"), 5))
    fps = max(1, _as_int(payload.get("fps"), 24))
    frames = _as_int(payload.get("num_frames"), duration * fps + 1)
    frames = max(17, min(frames, 241))

    args = [
        _env("MEDIA_RUNNER_PYTHON", sys.executable),
        str(script),
        "--prompt",
        str(payload.get("prompt") or ""),
        "--resolution",
        resolution,
        "--model_path",
        _env("HUNYUAN_MODEL_PATH"),
        "--aspect_ratio",
        str(payload.get("aspect_ratio") or "16:9"),
        "--video_length",
        str(frames),
        "--num_inference_steps",
        str(max(1, _as_int(payload.get("num_inference_steps") or payload.get("steps"), 50))),
        "--seed",
        str(_as_int(payload.get("seed"), 42)),
        "--dtype",
        str(payload.get("dtype") or _env("HUNYUAN_DTYPE", "bf16")),
        "--offloading",
        "true" if _as_bool(payload.get("offloading"), True) else "false",
        "--group_offloading",
        "true" if _as_bool(payload.get("group_offloading"), True) else "false",
        "--overlap_group_offloading",
        "true" if _as_bool(payload.get("overlap_group_offloading"), False) else "false",
        "--sr",
        "true" if _as_bool(payload.get("sr"), True) else "false",
        "--output_path",
        str(output_file),
    ]
    negative = str(payload.get("negative_prompt") or "").strip()
    if negative:
        args.extend(["--negative_prompt", negative])
    image = payload.get("image") or payload.get("image_path") or payload.get("start_image")
    if image:
        args.extend(["--image_path", _download_input(str(image))])
    if _as_bool(payload.get("enable_cache"), True):
        args.extend(
            [
                "--enable_cache",
                "--cache_type",
                str(payload.get("cache_type") or "deepcache"),
                "--cache_start_step",
                str(_as_int(payload.get("cache_start_step"), 10)),
                "--cache_end_step",
                str(_as_int(payload.get("cache_end_step"), 45)),
                "--cache_step_interval",
                str(_as_int(payload.get("cache_step_interval"), 3)),
            ]
        )
    return args


def _collect_outputs(output_dir: Path, output_file: Path, upstream: Path) -> list[str]:
    if output_file.is_file():
        return [output_file.name]
    candidates = list(output_dir.glob("*.mp4"))
    if not candidates:
        candidates = list(upstream.rglob("*.mp4"))
    if not candidates:
        return []
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    destination = output_dir / latest.name
    if latest.resolve() != destination.resolve():
        shutil.copy2(latest, destination)
    return [destination.name]


def main() -> int:
    request_path = Path(_env("MEDIA_REQUEST_JSON"))
    result_path = Path(_env("MEDIA_RESULT_JSON"))
    output_dir = Path(_env("MEDIA_OUTPUT_DIR", "/tmp/nexus-media-output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    upstream = Path(_env("MEDIA_UPSTREAM_DIR", "/data/app"))
    engine = _env("NEXUS_MEDIA_ENGINE").lower()
    payload: dict[str, Any] = json.loads(request_path.read_text(encoding="utf-8"))
    output_file = output_dir / "video.mp4"

    if engine == "ltx":
        args = _build_ltx(payload, output_file)
    elif engine == "hunyuan":
        args = _build_hunyuan(payload, output_file)
    else:
        raise RuntimeError(f"unsupported video engine: {engine}")

    proc = subprocess.run(args, cwd=str(upstream), check=False)
    videos = _collect_outputs(output_dir, output_file, upstream) if proc.returncode == 0 else []
    result = {
        "status": "ok" if proc.returncode == 0 and videos else "error",
        "returncode": proc.returncode,
        "engine": engine,
        "videos": videos,
        "args": args,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "ok" else (proc.returncode or 2)


if __name__ == "__main__":
    raise SystemExit(main())
