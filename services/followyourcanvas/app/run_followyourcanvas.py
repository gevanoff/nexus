import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _now() -> int:
    return int(time.time())


def _load_request() -> Dict[str, Any]:
    req_path = _env("FYC_REQUEST_JSON")
    if not req_path:
        raise RuntimeError("FYC_REQUEST_JSON is not set")
    return json.loads(Path(req_path).read_text(encoding="utf-8"))


def _safe_relpath(path: str) -> str:
    cleaned = path.strip().lstrip("/")
    if not cleaned or cleaned.startswith("..") or "/../" in cleaned or "\\" in cleaned:
        raise ValueError(f"unsafe path: {path!r}")
    return cleaned


def _resolve_under_workdir(workdir: Path, rel: str) -> Path:
    full = (workdir / _safe_relpath(rel)).resolve()
    root = workdir.resolve()
    if full != root and root not in full.parents:
        raise ValueError("path escapes workdir")
    return full


def _copy_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _write_output_json(output_path: Optional[Path], result: Dict[str, Any]) -> None:
    if output_path is None:
        return
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _bootstrap_script() -> Path:
    return Path(__file__).with_name("bootstrap_followyourcanvas.py")


def main() -> int:
    workdir = Path(_env("FYC_WORKDIR", "/data/app") or "/data/app")
    output_dir = Path(_env("FYC_OUTPUT_DIR") or "")
    if not output_dir:
        raise RuntimeError("FYC_OUTPUT_DIR is not set")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_json = _env("FYC_OUTPUT_JSON")
    output_json_path = Path(output_json) if output_json else None

    payload = _load_request()
    config_rel = payload.get("config") or _env("FYC_DEFAULT_CONFIG")
    if not config_rel:
        result = {
            "ok": False,
            "error": "missing_config",
            "detail": "Provide request key 'config' or set FYC_DEFAULT_CONFIG.",
        }
        _write_output_json(output_json_path, result)
        print(json.dumps(result))
        return 2

    mode = str(payload.get("mode") or _env("FYC_MODE", "with_prompt") or "with_prompt")
    if mode not in {"with_prompt", "no_prompt"}:
        result = {"ok": False, "error": "invalid_mode", "detail": "mode must be with_prompt|no_prompt"}
        _write_output_json(output_json_path, result)
        print(json.dumps(result))
        return 2

    script_rel = payload.get("script")
    if not script_rel:
        script_rel = "inference_outpainting-dir-with-prompt.py" if mode == "with_prompt" else "inference_outpainting-dir.py"

    extra_args: List[str] = []
    if isinstance(payload.get("extra_args"), list):
        extra_args = [str(item) for item in payload["extra_args"]]

    config_path = _resolve_under_workdir(workdir, str(config_rel))
    script_path = _resolve_under_workdir(workdir, str(script_rel))

    bootstrap_path = _bootstrap_script()
    if not script_path.exists() or not config_path.exists() or not bootstrap_path.exists():
        result = {
            "ok": False,
            "error": "missing_upstream_asset",
            "script": str(script_path),
            "config": str(config_path),
            "bootstrap": str(bootstrap_path),
        }
        _write_output_json(output_json_path, result)
        print(json.dumps(result))
        return 2

    cmd = [sys.executable, str(bootstrap_path), str(script_path), "--config", str(config_path), *extra_args]
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(workdir),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    elapsed_ms = int((time.time() - started) * 1000)

    copied: List[str] = []
    infer_dir = workdir / "infer"
    if infer_dir.is_dir():
        dst = output_dir / "infer"
        _copy_dir(infer_dir, dst)
        copied.append("infer/")

    result = {
        "ok": proc.returncode == 0,
        "created": _now(),
        "elapsed_ms": elapsed_ms,
        "workdir": str(workdir),
        "script": str(script_rel),
        "config": str(config_rel),
        "command": [shlex.quote(item) for item in cmd],
        "copied": copied,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }

    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    (output_dir / "stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
    _write_output_json(output_json_path, result)
    print(json.dumps({"ok": proc.returncode == 0, "elapsed_ms": elapsed_ms}))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
