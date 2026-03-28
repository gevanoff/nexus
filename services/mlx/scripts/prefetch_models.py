#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from huggingface_hub import snapshot_download


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prefetch MLX model repositories into the Hugging Face cache before starting mlx-openai-server."
    )
    parser.add_argument("--config", help="Path to mlx-openai-server config YAML")
    parser.add_argument("--model", action="append", default=[], help="Explicit model repo/path to prefetch (repeatable)")
    parser.add_argument("--cache-dir", default=os.environ.get("HF_HOME") or "", help="Optional Hugging Face cache directory")
    parser.add_argument("--check-only", action="store_true", help="Resolve models and print actions without downloading")
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
    cache_dir = args.cache_dir or None

    for model in models:
        if _looks_local_path(model):
            print(f"SKIP local model path: {model}")
            continue

        if args.check_only:
            print(f"WOULD PREFETCH {model}")
            continue

        print(f"PREFETCH {model}")
        try:
            snapshot_download(
                repo_id=model,
                cache_dir=cache_dir,
                token=token,
                resume_download=True,
            )
            print(f"OK {model}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {model}: {type(exc).__name__}: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
