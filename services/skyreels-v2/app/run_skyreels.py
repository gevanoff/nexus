import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _download_to_tmp(url: str, suffix: str) -> Path:
    import urllib.request

    tmpdir = Path(tempfile.mkdtemp(prefix="skyreels-download-"))
    dst = tmpdir / f"input{suffix}"
    with urllib.request.urlopen(url) as resp:
        dst.write_bytes(resp.read())
    return dst


def _resolve_media_path(value: str, suffix: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return str(_download_to_tmp(value, suffix))
    return value


def _infer_mode(payload: Dict[str, Any]) -> str:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode in {"df", "diffusion_forcing", "diffusion-forcing"}:
        return "df"
    if any(key in payload for key in ("base_num_frames", "ar_step", "overlap_history", "addnoise_condition")):
        return "df"
    return "standard"


def _normalize_resolution(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return value
        lowered = raw.lower()
        if lowered in {"540p", "540"}:
            return "540P"
        if lowered in {"720p", "720"}:
            return "720P"
        if lowered.endswith("p") and lowered[:-1].isdigit():
            return lowered[:-1] + "P"
    return value


def _normalize_resolution_from_dimensions(width: Any, height: Any) -> Optional[str]:
    w = _as_int(width)
    h = _as_int(height)
    if not w or not h:
        return None
    longest = max(w, h)
    if longest >= 1280:
        return "720P"
    return "540P"


def _default_model_id(mode: str, payload: Dict[str, Any]) -> str:
    if mode == "df":
        return "Skywork/SkyReels-V2-DF-1.3B-540P"
    if payload.get("image") or payload.get("image_path") or payload.get("start_image"):
        return "Skywork/SkyReels-V2-I2V-1.3B-540P"
    return "Skywork/SkyReels-V2-T2V-14B-540P"


def _normalize_num_frames(payload: Dict[str, Any]) -> Optional[int]:
    num_frames = _as_int(payload.get("num_frames"))
    if num_frames is not None and num_frames > 0:
        return num_frames

    duration_seconds = _as_int(payload.get("duration_seconds"))
    fps = _as_int(payload.get("fps")) or 8
    if duration_seconds is None or duration_seconds <= 0:
        return None
    return max(1, duration_seconds * fps)


def _build_args(payload: Dict[str, Any], outdir: Path) -> List[str]:
    workdir = Path(_env("SKYREELS_WORKDIR", "/data/app"))
    mode = _infer_mode(payload)
    script_name = "generate_video_df.py" if mode == "df" else "generate_video.py"
    script_path = workdir / script_name
    if not script_path.exists():
        raise RuntimeError(f"Missing SkyReels script: {script_path}")

    args: List[str] = [sys.executable, str(script_path)]

    def add_flag(flag: str, value: Any) -> None:
        if value is None or value == "":
            return
        args.extend([flag, str(value)])

    resolution = (
        _normalize_resolution(payload.get("resolution"))
        or _normalize_resolution_from_dimensions(payload.get("width"), payload.get("height"))
        or "540P"
    )
    normalized_num_frames = _normalize_num_frames(payload)
    add_flag("--model_id", payload.get("model_id") or _default_model_id(mode, payload))
    add_flag("--resolution", resolution)
    add_flag("--prompt", payload.get("prompt"))

    image = payload.get("image") or payload.get("image_path") or payload.get("start_image")
    if image:
        add_flag("--image", _resolve_media_path(str(image), ".png"))
    end_image = payload.get("end_image")
    if end_image:
        add_flag("--end_image", _resolve_media_path(str(end_image), ".png"))
    video_path = payload.get("video_path") or payload.get("video")
    if video_path:
        add_flag("--video_path", _resolve_media_path(str(video_path), ".mp4"))

    if mode == "df":
        add_flag("--ar_step", payload.get("ar_step"))
        add_flag("--base_num_frames", payload.get("base_num_frames") or normalized_num_frames)
        add_flag("--num_frames", normalized_num_frames)
        add_flag("--overlap_history", payload.get("overlap_history"))
        add_flag("--addnoise_condition", payload.get("addnoise_condition"))
        add_flag("--guidance_scale", payload.get("guidance_scale"))
        add_flag("--shift", payload.get("shift"))
        add_flag("--fps", payload.get("fps"))
        add_flag("--seed", payload.get("seed"))
        add_flag("--outdir", str(outdir))
        ar_step = _as_int(payload.get("ar_step"))
        if ar_step and ar_step > 0:
            add_flag("--causal_block_size", payload.get("causal_block_size"))
    else:
        add_flag("--num_frames", normalized_num_frames)
        add_flag("--guidance_scale", payload.get("guidance_scale"))
        add_flag("--shift", payload.get("shift"))
        add_flag("--fps", payload.get("fps"))
        add_flag("--seed", payload.get("seed"))
        add_flag("--outdir", str(outdir))

    if _bool(payload.get("offload")):
        args.append("--offload")
    if _bool(payload.get("teacache")):
        args.append("--teacache")
    if _bool(payload.get("use_ret_steps")):
        args.append("--use_ret_steps")
    teacache_thresh = _as_float(payload.get("teacache_thresh"))
    if teacache_thresh is not None:
        add_flag("--teacache_thresh", teacache_thresh)

    return args


def _collect_outputs(outdir: Path, workdir: Path) -> None:
    if any(outdir.glob("*.mp4")) or any(outdir.glob("*.webm")):
        return
    candidates = list(workdir.rglob("*.mp4")) + list(workdir.rglob("*.webm"))
    if not candidates:
        return
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    shutil.copy2(latest, outdir / latest.name)


def _list_videos(outdir: Path) -> List[str]:
    videos = [*outdir.glob("*.mp4"), *outdir.glob("*.webm")]
    videos.sort(key=lambda item: item.name)
    return [item.name for item in videos]


def _write_result(outdir: Path, output_json: Optional[Path], payload: Dict[str, Any], args: List[str], returncode: int, status: str) -> None:
    result = {
        "status": status,
        "returncode": returncode,
        "timestamp": int(time.time()),
        "args": args,
        "payload_keys": sorted(str(key) for key in payload.keys()),
        "videos": _list_videos(outdir),
    }
    (outdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_json is not None:
        output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    request_json = _env("SKYREELS_REQUEST_JSON")
    if not request_json:
        raise RuntimeError("SKYREELS_REQUEST_JSON not set")
    output_dir = Path(_env("SKYREELS_OUTPUT_DIR", "/tmp/skyreels-output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_json_raw = _env("SKYREELS_OUTPUT_JSON")
    output_json = Path(output_json_raw) if output_json_raw else None
    workdir = Path(_env("SKYREELS_WORKDIR", "/data/app"))

    payload: Dict[str, Any] = json.loads(Path(request_json).read_text(encoding="utf-8"))
    args = _build_args(payload, output_dir)
    proc = subprocess.run(args, cwd=str(workdir))
    if proc.returncode != 0:
        _write_result(output_dir, output_json, payload, args, proc.returncode, "error")
        return proc.returncode

    _collect_outputs(output_dir, workdir)
    videos = _list_videos(output_dir)
    if not videos:
        _write_result(output_dir, output_json, payload, args, 0, "no_output")
        sys.stderr.write("SkyReels wrapper: subprocess returned 0 but no output video was found.\n")
        return 2

    _write_result(output_dir, output_json, payload, args, 0, "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
