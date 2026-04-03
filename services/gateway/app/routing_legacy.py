from __future__ import annotations

import json
from typing import List

from app.backends import get_registry
from app.config import S
from app.models import ChatMessage


def choose_backend(model: str) -> str:
    m = (model or "").strip()
    prefix = m.split(":", 1)[0].strip() if ":" in m else ""
    registry = get_registry()
    if prefix:
        resolved = registry.resolve_backend_class(prefix)
        if registry.get_backend(resolved) is not None:
            return resolved

    lowered = m.lower()
    if lowered in {"ollama", "ollama-default", "vllm", "vllm-default", "local_vllm", "local-vllm"}:
        return registry.resolve_backend_class("vllm")
    if lowered in {"mlx", "mlx-default", "local_mlx", "local-mlx"}:
        return registry.resolve_backend_class("mlx")

    return registry.resolve_backend_class(S.DEFAULT_BACKEND)


def normalize_model(model: str, backend: str) -> str:
    m = (model or "").strip()
    for prefix in {
        backend,
        backend.replace("_", "-"),
        backend.replace("-", "_"),
        "vllm",
        "local_vllm",
        "local-vllm",
        "mlx",
        "local_mlx",
        "local-mlx",
        "ollama",
    }:
        if prefix and m.lower().startswith(prefix.lower() + ":"):
            m = m[len(prefix) + 1 :]
            break

    if m in {"default", "vllm", "vllm-default", "local_vllm", "local-vllm", "ollama", "ollama-default", ""}:
        return S.VLLM_MODEL_DEFAULT
    if m in {"mlx", "local_mlx", "local-mlx"}:
        return S.MLX_MODEL_DEFAULT
    return m


def approx_text_size(messages: List[ChatMessage]) -> int:
    n = 0
    for m in messages:
        c = m.content
        if isinstance(c, str):
            n += len(c)
        elif c is None:
            continue
        else:
            n += len(json.dumps(c))
    return n
