from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_public_http_url(value: str) -> tuple[str, int, str]:
    """Resolve a remote input once and return a public address pinned for curl.

    The caller must connect using the returned address rather than resolving the
    hostname again. This closes the DNS-rebinding window between validation and
    download while preserving the original hostname for HTTP Host and TLS SNI.
    """

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("image URL must use http or https and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("image URL credentials are not allowed")

    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise ValueError("image URL must not target localhost")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"image URL hostname could not be resolved: {hostname}") from exc
    if not addresses:
        raise ValueError(f"image URL hostname returned no addresses: {hostname}")

    public_addresses: list[str] = []
    for address in addresses:
        ip_text = address[4][0]
        ip = ipaddress.ip_address(ip_text)
        if not ip.is_global:
            raise ValueError(f"image URL resolved to a non-public address: {ip}")
        normalized = str(ip)
        if normalized not in public_addresses:
            public_addresses.append(normalized)

    return hostname, port, public_addresses[0]


def _curl_resolve_value(hostname: str, port: int, ip_text: str) -> str:
    ip = ipaddress.ip_address(ip_text)
    address = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"{hostname}:{port}:{address}"


def _download_input(value: str, temp_dir: Path, suffix: str = ".png") -> str:
    if not value.startswith(("http://", "https://")):
        root = Path(_env("MEDIA_INPUT_ROOT", "/data/inputs")).resolve()
        source = Path(value).expanduser().resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError(f"local image input must be a file under {root}")
        return str(source)

    hostname, port, resolved_ip = _resolve_public_http_url(value)
    destination = temp_dir / f"input{suffix}"
    max_bytes = max(1, _as_int(_env("MEDIA_MAX_INPUT_BYTES"), 25_000_000))
    destination.unlink(missing_ok=True)

    command = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "30",
        "--max-time",
        "120",
        "--max-filesize",
        str(max_bytes),
        "--proto",
        "=http,https",
        "--proto-redir",
        "=",
        "--noproxy",
        "*",
        "--resolve",
        _curl_resolve_value(hostname, port, resolved_ip),
        "--output",
        str(destination),
        value,
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("curl is required to download remote image inputs safely") from exc

    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        detail = (proc.stderr or proc.stdout or f"curl exited {proc.returncode}").strip()
        raise ValueError(f"image input download failed: {detail[-1000:]}")
    if not destination.is_file():
        raise ValueError("image input download produced no file")
    if destination.stat().st_size > max_bytes:
        destination.unlink(missing_ok=True)
        raise ValueError(f"image input exceeds {max_bytes} bytes")
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

    return [
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


def _build_hunyuan(payload: dict[str, Any], output_file: Path, temp_dir: Path) -> list[str]:
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
        args.extend(["--image_path", _download_input(str(image), temp_dir)])
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
    request_path = Path(_required_env("MEDIA_REQUEST_JSON"))
    result_path = Path(_required_env("MEDIA_RESULT_JSON"))
    if not request_path.is_file():
        raise RuntimeError(f"MEDIA_REQUEST_JSON is not a readable file: {request_path}")
    result_path.parent.mkdir(parents=True, exist_ok=True)

    output_dir = Path(_env("MEDIA_OUTPUT_DIR", "/tmp/nexus-media-output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    upstream = Path(_env("MEDIA_UPSTREAM_DIR", "/data/app"))
    engine = _required_env("NEXUS_MEDIA_ENGINE").lower()
    payload: dict[str, Any] = json.loads(request_path.read_text(encoding="utf-8"))
    output_file = output_dir / "video.mp4"

    with tempfile.TemporaryDirectory(prefix="nexus-media-input-") as input_temp:
        if engine == "ltx":
            args = _build_ltx(payload, output_file)
        elif engine == "hunyuan":
            args = _build_hunyuan(payload, output_file, Path(input_temp))
        else:
            raise RuntimeError(f"unsupported video engine: {engine}")
        proc = subprocess.run(args, cwd=str(upstream), check=False)

    videos = _collect_outputs(output_dir, output_file, upstream) if proc.returncode == 0 else []
    result = {
        "status": "ok" if proc.returncode == 0 and videos else "error",
        "returncode": proc.returncode,
        "engine": engine,
        "videos": videos,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "ok" else (proc.returncode or 2)


if __name__ == "__main__":
    raise SystemExit(main())
