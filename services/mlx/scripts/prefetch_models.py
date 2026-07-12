#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

# This helper is intentionally the online cache-fill boundary. Set the mode
# before importing huggingface_hub, which snapshots this setting at import.
os.environ["HF_HUB_OFFLINE"] = "0"

import yaml
from huggingface_hub import HfApi, snapshot_download
from verify_model_snapshot import verify_snapshot


SHARD_RE = re.compile(r"^(?P<prefix>.+)-(?P<index>\d+)-of-(?P<total>\d+)\.(?:safetensors|bin)$")
STATUS_FILE = ".nexus_download_status.json"
LOCK_FILE = ".nexus_download.lock"


def _normalize_model_path(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    return os.path.expanduser(value)


def _looks_local_path(value: str) -> bool:
    if not value:
        return False
    path = Path(value)
    if path.is_absolute():
        return True
    if value.startswith("./") or value.startswith("../") or value.startswith("~/"):
        return True
    return path.exists()


def _collect_models_from_config(config_path: str) -> list[str]:
    with open(config_path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    models: list[str] = []
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return models

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_path = _normalize_model_path(str(entry.get("model_path") or "").strip())
        if model_path:
            models.append(model_path)
    return models


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _cache_repo_path(cache_dir: str, model: str) -> Path:
    return Path(cache_dir) / f"models--{model.replace('/', '--')}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _acquire_job_lock(repo_path: Path) -> Path:
    repo_path.mkdir(parents=True, exist_ok=True)
    lock_path = repo_path / LOCK_FILE
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except Exception:
                owner_pid = 0
            if _process_is_alive(owner_pid):
                raise RuntimeError(f"download already active with pid {owner_pid}")
            lock_path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return lock_path
    raise RuntimeError("could not acquire model download lock")


def _remote_shards(model: str, token: str | None) -> tuple[str, list[str]]:
    info = HfApi(token=token).model_info(repo_id=model)
    revision = str(getattr(info, "sha", "") or "").strip()
    shards = []
    for sibling in getattr(info, "siblings", None) or []:
        relative = str(getattr(sibling, "rfilename", "") or "").strip()
        if relative and SHARD_RE.match(Path(relative).name):
            shards.append(relative)
    return revision, sorted(set(shards))


def _shard_progress(repo_path: Path, revision: str, expected_shards: list[str]) -> tuple[int, int, int]:
    snapshots_root = repo_path / "snapshots"
    snapshots: list[Path] = []
    if revision:
        if (snapshots_root / revision).is_dir():
            snapshots.append(snapshots_root / revision)
    elif snapshots_root.is_dir():
        snapshots.extend(path for path in snapshots_root.iterdir() if path.is_dir() and path not in snapshots)

    if expected_shards:
        downloaded = sum(
            1
            for relative in expected_shards
            if any((snapshot / relative).is_file() for snapshot in snapshots)
        )
        expected = len(expected_shards)
    else:
        groups: dict[tuple[str, int], set[int]] = {}
        for snapshot in snapshots:
            for path in snapshot.rglob("*"):
                match = SHARD_RE.match(path.name)
                if not match or not path.is_file():
                    continue
                total = int(match.group("total"))
                groups.setdefault((match.group("prefix"), total), set()).add(int(match.group("index")))
        downloaded = sum(len(present) for present in groups.values())
        expected = sum(total for (_prefix, total) in groups) if groups else 0

    incomplete_bytes = 0
    blobs = repo_path / "blobs"
    if blobs.is_dir():
        for partial in blobs.glob("*.incomplete"):
            try:
                incomplete_bytes += partial.stat().st_size
            except OSError:
                continue
    return downloaded, expected, incomplete_bytes


class DownloadStatus:
    def __init__(
        self,
        *,
        model: str,
        repo_path: Path,
        revision: str,
        expected_shards: list[str],
        max_attempts: int,
    ) -> None:
        now = time.time()
        self.repo_path = repo_path
        self.revision = revision
        self.expected_shards = expected_shards
        self.path = repo_path / STATUS_FILE
        self.lock = threading.Lock()
        self.payload: dict[str, Any] = {
            "version": 1,
            "model": model,
            "state": "starting",
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "last_progress_at": now,
            "attempt": 0,
            "max_attempts": max_attempts,
            "retry_count": 0,
            "revision": revision,
            "downloaded_shards": 0,
            "expected_shards": len(expected_shards),
            "incomplete_bytes": 0,
            "next_retry_at": 0,
            "error": "",
        }
        self._write()

    def _write(self) -> None:
        _atomic_write_json(self.path, self.payload)

    def update(self, **fields: Any) -> None:
        with self.lock:
            self.payload.update(fields)
            self.payload["updated_at"] = time.time()
            self._write()

    def refresh_progress(self) -> None:
        downloaded, expected, incomplete_bytes = _shard_progress(
            self.repo_path,
            self.revision,
            self.expected_shards,
        )
        with self.lock:
            previous = (
                int(self.payload.get("downloaded_shards") or 0),
                int(self.payload.get("incomplete_bytes") or 0),
            )
            current = (downloaded, incomplete_bytes)
            self.payload["downloaded_shards"] = downloaded
            self.payload["expected_shards"] = expected
            self.payload["incomplete_bytes"] = incomplete_bytes
            self.payload["updated_at"] = time.time()
            if current != previous:
                self.payload["last_progress_at"] = self.payload["updated_at"]
            self._write()


def _monitor_download(status: DownloadStatus, stop: threading.Event, interval_sec: float) -> None:
    while not stop.wait(interval_sec):
        try:
            status.refresh_progress()
        except Exception:
            continue


def _retry_delay(attempt: int, base_sec: float, max_sec: float) -> float:
    return min(max_sec, base_sec * (2 ** max(0, attempt - 1)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prefetch MLX model repositories into the Hugging Face cache before starting mlx-openai-server."
    )
    parser.add_argument("--config", help="Path to mlx-openai-server config YAML")
    parser.add_argument("--model", action="append", default=[], help="Explicit model repo/path to prefetch (repeatable)")
    parser.add_argument("--cache-dir", default=os.environ.get("HF_HOME") or "", help="Optional Hugging Face cache directory")
    parser.add_argument("--check-only", action="store_true", help="Resolve models and print actions without downloading")
    parser.add_argument("--max-attempts", type=int, default=int(os.environ.get("MLX_PREFETCH_MAX_ATTEMPTS", "5")))
    parser.add_argument("--retry-base-sec", type=float, default=float(os.environ.get("MLX_PREFETCH_RETRY_BASE_SEC", "30")))
    parser.add_argument("--retry-max-sec", type=float, default=float(os.environ.get("MLX_PREFETCH_RETRY_MAX_SEC", "300")))
    parser.add_argument("--progress-interval-sec", type=float, default=float(os.environ.get("MLX_PREFETCH_PROGRESS_INTERVAL_SEC", "5")))
    args = parser.parse_args()

    models: list[str] = []
    if args.config:
        if not os.path.isfile(args.config):
            print(f"ERROR: config file not found: {args.config}", file=sys.stderr)
            return 2
        models.extend(_collect_models_from_config(args.config))
    models.extend(_normalize_model_path(item) for item in args.model or [])
    models = _unique([item for item in models if item])

    if not models:
        print("ERROR: no models resolved from --config or --model", file=sys.stderr)
        return 2

    failures = 0
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or None
    cache_dir = args.cache_dir or os.environ.get("HF_HOME") or ""
    max_attempts = max(1, args.max_attempts)
    retry_base_sec = max(1.0, args.retry_base_sec)
    retry_max_sec = max(retry_base_sec, args.retry_max_sec)
    progress_interval_sec = max(1.0, args.progress_interval_sec)

    for model in models:
        if _looks_local_path(model):
            print(f"SKIP local model path: {model}")
            continue

        if args.check_only:
            print(f"WOULD PREFETCH {model}")
            continue

        print(f"PREFETCH {model}", flush=True)
        if not cache_dir:
            print(f"ERROR {model}: HF_HOME or --cache-dir is required for tracked downloads", file=sys.stderr)
            failures += 1
            continue

        repo_path = _cache_repo_path(cache_dir, model)
        lock_path: Path | None = None
        status: DownloadStatus | None = None
        stop_monitor = threading.Event()
        monitor: threading.Thread | None = None
        try:
            lock_path = _acquire_job_lock(repo_path)
            revision = ""
            expected_shards: list[str] = []
            try:
                revision, expected_shards = _remote_shards(model, token)
            except Exception as exc:
                print(f"WARN {model}: shard manifest unavailable: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

            status = DownloadStatus(
                model=model,
                repo_path=repo_path,
                revision=revision,
                expected_shards=expected_shards,
                max_attempts=max_attempts,
            )
            monitor = threading.Thread(
                target=_monitor_download,
                args=(status, stop_monitor, progress_interval_sec),
                name=f"prefetch-progress-{model}",
                daemon=True,
            )
            monitor.start()

            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                status.update(
                    state="downloading",
                    attempt=attempt,
                    retry_count=attempt - 1,
                    next_retry_at=0,
                    error="",
                )
                try:
                    snapshot_path = snapshot_download(
                        repo_id=model,
                        cache_dir=cache_dir,
                        token=token,
                        resume_download=True,
                    )
                    snapshot_errors = verify_snapshot(Path(snapshot_path))
                    if snapshot_errors:
                        raise RuntimeError("; ".join(snapshot_errors))
                    status.refresh_progress()
                    status.update(state="complete", completed_at=time.time(), error="", next_retry_at=0)
                    print(f"OK {model}", flush=True)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    if attempt >= max_attempts:
                        status.update(state="failed", failed_at=time.time(), error=error, next_retry_at=0)
                        break
                    delay = _retry_delay(attempt, retry_base_sec, retry_max_sec)
                    status.update(
                        state="retry_wait",
                        error=error,
                        next_retry_at=time.time() + delay,
                    )
                    print(
                        f"RETRY {model}: attempt {attempt}/{max_attempts} failed; resuming in {delay:.0f}s: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(delay)
            if last_error is not None:
                raise last_error
        except Exception as exc:
            failures += 1
            if status is not None and status.payload.get("state") not in {"failed", "complete"}:
                status.update(state="failed", failed_at=time.time(), error=f"{type(exc).__name__}: {exc}"[:1000])
            print(f"ERROR {model}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        finally:
            stop_monitor.set()
            if monitor is not None:
                monitor.join(timeout=max(2.0, progress_interval_sec + 1.0))
            if lock_path is not None:
                lock_path.unlink(missing_ok=True)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
