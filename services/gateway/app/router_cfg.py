from __future__ import annotations

from app.config import S
from app.router import RouterConfig


def router_cfg() -> RouterConfig:
    return RouterConfig(
        default_backend=S.DEFAULT_BACKEND,
        primary_strong_model=S.MLX_MODEL_STRONG,
        primary_fast_model=S.VLLM_MODEL_FAST,
        long_context_chars_threshold=S.ROUTER_LONG_CONTEXT_CHARS,
    )
