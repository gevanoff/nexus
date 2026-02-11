from __future__ import annotations

from app.config import S
from app.router import RouterConfig


def router_cfg() -> RouterConfig:
    return RouterConfig(
        default_backend=S.DEFAULT_BACKEND,
        ollama_strong_model=S.OLLAMA_MODEL_STRONG,
        ollama_fast_model=S.OLLAMA_MODEL_FAST,
        mlx_strong_model=S.MLX_MODEL_STRONG,
        mlx_fast_model=S.MLX_MODEL_FAST,
        long_context_chars_threshold=S.ROUTER_LONG_CONTEXT_CHARS,
    )
