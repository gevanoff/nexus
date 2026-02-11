from __future__ import annotations

import json
from typing import List, Literal

from app.config import S
from app.models import ChatMessage


def choose_backend(model: str) -> Literal["ollama", "mlx"]:
    m = (model or "").strip().lower()

    if m.startswith("ollama:"):
        return "ollama"
    if m.startswith("mlx:"):
        return "mlx"

    if m in {"ollama", "ollama-default"}:
        return "ollama"
    if m in {"mlx", "mlx-default"}:
        return "mlx"

    return S.DEFAULT_BACKEND


def normalize_model(model: str, backend: str) -> str:
    m = (model or "").strip()

    if backend == "ollama":
        if m.startswith("ollama:"):
            m = m[len("ollama:") :]
        if m in {"default", "ollama", ""}:
            return S.OLLAMA_MODEL_DEFAULT
        return m

    if m.startswith("mlx:"):
        m = m[len("mlx:") :]
    if m in {"default", "mlx", ""}:
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
