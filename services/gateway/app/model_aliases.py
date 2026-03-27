from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.backends import backend_provider_name
from app.config import S


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class ModelAlias:
    backend: str
    upstream_model: str
    context_window: Optional[int] = None
    tools: Optional[bool] = None
    max_tokens_cap: Optional[int] = None
    temperature_cap: Optional[float] = None


@dataclass(frozen=True)
class AliasLoadState:
    source: str
    configured_path: str = ""
    error: str = ""


def _default_aliases() -> Dict[str, ModelAlias]:
    # Sensible defaults if no explicit config is provided.
    default_backend = S.DEFAULT_BACKEND

    def strong_for(backend: str) -> str:
        return S.OLLAMA_MODEL_STRONG if backend_provider_name(backend) == "ollama" else S.MLX_MODEL_STRONG

    def fast_for(backend: str) -> str:
        return S.OLLAMA_MODEL_FAST if backend_provider_name(backend) == "ollama" else S.MLX_MODEL_FAST

    return {
        # These four are the canonical policy surface.
        "default": ModelAlias(backend=default_backend, upstream_model=strong_for(default_backend), tools=True),
        "fast": ModelAlias(backend=default_backend, upstream_model=fast_for(default_backend), tools=False),
        "coder": ModelAlias(backend="ollama", upstream_model=S.OLLAMA_MODEL_STRONG, tools=True),
        "long": ModelAlias(
            backend="mlx",
            upstream_model=S.MLX_MODEL_STRONG,
            context_window=S.ROUTER_LONG_CONTEXT_CHARS,
            tools=False,
        ),
    }


def _parse_alias_value(v: Any) -> Optional[ModelAlias]:
    # Accept either:
    # - "ollama:qwen3:30b" / "mlx:..."
    # - {"backend": "ollama", "model": "qwen3:30b", "context": 8192}
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.startswith("ollama:"):
            return ModelAlias(backend="ollama", upstream_model=s[len("ollama:") :])
        if s.startswith("mlx:"):
            return ModelAlias(backend="mlx", upstream_model=s[len("mlx:") :])
        return None

    if isinstance(v, dict):
        backend = (v.get("backend") or "").strip()
        model = (v.get("model") or v.get("upstream_model") or "").strip()
        if not backend or not model:
            return None
        if model.startswith("ollama:"):
            model = model[len("ollama:") :]
        elif model.startswith("mlx:"):
            model = model[len("mlx:") :]

        context = v.get("context") or v.get("context_window") or v.get("window")
        context_window: Optional[int] = None
        if isinstance(context, int) and context > 0:
            context_window = context
        tools_raw = v.get("tools")
        tools: Optional[bool] = None
        if isinstance(tools_raw, bool):
            tools = tools_raw

        mt = v.get("max_tokens_cap") or v.get("max_tokens") or v.get("max_output_tokens")
        max_tokens_cap: Optional[int] = None
        if isinstance(mt, int) and mt > 0:
            max_tokens_cap = mt

        tc = v.get("temperature_cap") or v.get("temp_cap")
        temperature_cap: Optional[float] = None
        if isinstance(tc, (int, float)) and tc >= 0:
            temperature_cap = float(tc)

        return ModelAlias(
            backend=backend,
            upstream_model=model,
            context_window=context_window,
            tools=tools,
            max_tokens_cap=max_tokens_cap,
            temperature_cap=temperature_cap,
        )

    return None


def load_aliases() -> Dict[str, ModelAlias]:
    global _ALIASES_STATE

    aliases: Dict[str, ModelAlias] = dict(_default_aliases())

    raw_json = (S.MODEL_ALIASES_JSON or "").strip()
    path = (S.MODEL_ALIASES_PATH or "").strip()
    fallback_path = Path(__file__).with_name("model_aliases.json")

    payload: Any = None
    source = "defaults"
    error = ""
    if raw_json:
        try:
            payload = json.loads(raw_json)
            source = "env:MODEL_ALIASES_JSON"
        except Exception:
            payload = None
            error = "MODEL_ALIASES_JSON could not be parsed"
    else:
        if path:
            candidate = Path(path)
            if candidate.exists():
                try:
                    with candidate.open("r", encoding="utf-8") as f:
                        payload = json.load(f)
                    source = f"path:{candidate}"
                except Exception as exc:
                    payload = None
                    error = f"MODEL_ALIASES_PATH unreadable: {candidate} ({type(exc).__name__}: {exc})"
            else:
                error = f"MODEL_ALIASES_PATH not found: {candidate}"
        elif fallback_path.exists():
            try:
                with fallback_path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                source = f"path:{fallback_path}"
            except Exception as exc:
                payload = None
                error = f"fallback aliases unreadable: {fallback_path} ({type(exc).__name__}: {exc})"

    if isinstance(payload, dict) and isinstance(payload.get("aliases"), dict):
        payload = payload["aliases"]

    if isinstance(payload, dict):
        for k, v in payload.items():
            if not isinstance(k, str):
                continue
            parsed = _parse_alias_value(v)
            if parsed:
                aliases[k.strip().lower()] = parsed

    _ALIASES_STATE = AliasLoadState(source=source, configured_path=path, error=error)
    if error:
        logger.warning("model aliases: source=%s configured_path=%s error=%s", source, path or "-", error)
    else:
        logger.info("model aliases: source=%s configured_path=%s count=%d", source, path or "-", len(aliases))

    return aliases


_ALIASES_CACHE: Optional[Dict[str, ModelAlias]] = None
_ALIASES_STATE: AliasLoadState = AliasLoadState(source="defaults")


def get_aliases() -> Dict[str, ModelAlias]:
    """Load aliases once per process.

    This keeps routing deterministic and cheap per request.
    To change aliases, update the JSON file/env and restart the gateway.
    """

    global _ALIASES_CACHE
    if _ALIASES_CACHE is None:
        _ALIASES_CACHE = load_aliases()
    return _ALIASES_CACHE


def get_aliases_state() -> AliasLoadState:
    if _ALIASES_CACHE is None:
        get_aliases()
    return _ALIASES_STATE


def resolve_alias(model: str) -> Optional[Tuple[str, str]]:
    m = (model or "").strip().lower()
    if not m:
        return None
    a = get_aliases().get(m)
    if not a:
        return None
    return a.backend, a.upstream_model


def get_alias(alias_name: str) -> Optional[ModelAlias]:
    k = (alias_name or "").strip().lower()
    if not k:
        return None
    return get_aliases().get(k)
