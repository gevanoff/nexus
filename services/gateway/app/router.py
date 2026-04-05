from __future__ import annotations

import json
from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, Optional

from app.backends import backend_provider_name, get_registry
from app.config import S
from app.model_aliases import get_alias, get_aliases

Backend = str


@dataclass(frozen=True)
class RouteDecision:
    backend: Backend
    model: str
    reason: str


@dataclass(frozen=True)
class RouterConfig:
    default_backend: Backend

    # Model choices for the primary OpenAI-compatible chat tier.
    primary_strong_model: str
    primary_fast_model: str

    # Heuristic thresholds
    long_context_chars_threshold: int = 40_000


def _approx_text_size(messages: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = (m or {}).get("content")
        if isinstance(c, str):
            n += len(c)
        elif c is None:
            continue
        else:
            try:
                n += len(json.dumps(c))
            except Exception:
                n += 0
    return n


def _resolved_backend_name(name: str) -> str:
    registry = get_registry()
    return registry.resolve_backend_class((name or "").strip())


def _known_backend_name(name: str) -> Optional[str]:
    registry = get_registry()
    resolved = registry.resolve_backend_class((name or "").strip())
    if resolved and registry.get_backend(resolved) is not None:
        return resolved
    return None


def _provider_default_backend(provider: str) -> str:
    registry = get_registry()
    if provider == "vllm":
        resolved = registry.resolve_backend_class("vllm")
        return resolved or "local_vllm"
    if provider == "mlx":
        resolved = registry.resolve_backend_class("mlx")
        return resolved or "local_mlx"
    return provider


def _backend_prefixes(backend: str) -> list[str]:
    resolved = _resolved_backend_name(backend)
    provider = backend_provider_name(resolved or backend)
    prefixes = {
        backend,
        resolved,
        (backend or "").replace("_", "-"),
        (backend or "").replace("-", "_"),
        (resolved or "").replace("_", "-"),
        (resolved or "").replace("-", "_"),
    }
    if provider == "vllm":
        prefixes.update({"vllm", "local_vllm", "local-vllm", _provider_default_backend("vllm")})
    if provider == "mlx":
        prefixes.update({"mlx", "local_mlx", "local-mlx", _provider_default_backend("mlx")})
    return [p for p in prefixes if isinstance(p, str) and p]


def _default_model_for_backend(backend: str, cfg: RouterConfig) -> str:
    resolved = _resolved_backend_name(backend) or backend
    if resolved == "local_vllm_fast":
        return (getattr(S, "VLLM_MODEL_FAST", "") or "").strip() or cfg.primary_fast_model
    if resolved == "local_vllm_embeddings":
        return (getattr(S, "VLLM_MODEL_EMBEDDINGS", "") or "").strip() or cfg.primary_fast_model
    if resolved == "local_vllm":
        return (getattr(S, "VLLM_MODEL_STRONG", "") or "").strip() or cfg.primary_fast_model
    if resolved == "local_mlx":
        return (getattr(S, "MLX_MODEL_STRONG", "") or "").strip() or cfg.primary_strong_model

    provider = backend_provider_name(resolved)
    if provider == "mlx":
        return (getattr(S, "MLX_MODEL_STRONG", "") or "").strip() or cfg.primary_strong_model
    if provider == "vllm":
        return (getattr(S, "VLLM_MODEL_STRONG", "") or "").strip() or cfg.primary_fast_model
    return cfg.primary_strong_model


def _backend_from_model_prefix(model: str) -> Optional[str]:
    m = (model or "").strip()
    if ":" not in m:
        return None
    prefix, _rest = m.split(":", 1)
    return _known_backend_name(prefix.strip())


def _choose_backend_by_model(model: str, default_backend: Backend) -> Backend:
    m = (model or "").strip().lower()

    explicit = _backend_from_model_prefix(model)
    if explicit:
        return explicit

    if m in {"vllm", "vllm-default", "local_vllm", "local-vllm"}:
        return _provider_default_backend("vllm")
    if m in {"mlx", "mlx-default", "local_mlx", "local-mlx"}:
        return _provider_default_backend("mlx")
    if m in {"ollama", "ollama-default"}:
        return _provider_default_backend("vllm")

    return _resolved_backend_name(default_backend) or default_backend


def _normalize_model(model: str, backend: Backend, cfg: RouterConfig) -> str:
    m = (model or "").strip()
    for prefix in _backend_prefixes(backend):
        if m.lower().startswith(prefix.lower() + ":"):
            m = m[len(prefix) + 1 :]
            break

    provider = backend_provider_name(backend)
    m_key = m.lower()
    if m_key in {prefix.lower() for prefix in _backend_prefixes(backend)}:
        return _default_model_for_backend(backend, cfg)
    if provider == "vllm" and m_key in {
        "default",
        "vllm",
        "vllm-default",
        "local_vllm",
        "local-vllm",
        "ollama",
        "ollama-default",
        "auto",
        "",
    }:
        return _default_model_for_backend(backend, cfg)
    if provider == "mlx" and m_key in {
        "default",
        "mlx",
        "mlx-default",
        "local_mlx",
        "local-mlx",
        "ollama",
        "ollama-default",
        "auto",
        "",
    }:
        return _default_model_for_backend(backend, cfg)
    return m


_CODE_HINT_RE = re.compile(
    r"\b(typescript|javascript|python|py|node|npm|pip|pytest|uvicorn|fastapi|dockerfile|kubernetes|terraform|ansible|git)\b",
    re.IGNORECASE,
)
_CODE_ERROR_RE = re.compile(
    r"\b(traceback|stack trace|exception|segmentation fault|syntaxerror|typeerror|valueerror|nullpointerexception|panic:)\b",
    re.IGNORECASE,
)
_CODE_EXT_RE = re.compile(r"\.(py|js|ts|tsx|jsx|java|go|rs|cs|cpp|cxx|hpp|h|sql|yaml|yml|toml|json)\b", re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(r"(^|\s)(def|class|import|from|function|const|let|var|public|private)\b")


def _last_user_text(messages: Iterable[Dict[str, Any]]) -> str:
    try:
        for m in reversed(list(messages)):
            if not isinstance(m, dict):
                continue
            if (m.get("role") or "").strip().lower() != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c
            if c is None:
                continue
            try:
                return json.dumps(c)
            except Exception:
                return ""
    except Exception:
        return ""
    return ""


def _is_probably_coding_request(messages: Iterable[Dict[str, Any]]) -> bool:
    text = (_last_user_text(messages) or "").strip()
    if not text:
        return False
    if "```" in text:
        return True
    if _CODE_ERROR_RE.search(text):
        return True
    if _CODE_EXT_RE.search(text):
        return True
    if _CODE_TOKEN_RE.search(text) and ("{" in text or ":" in text or "(" in text):
        return True
    if _CODE_HINT_RE.search(text) and ("error" in text.lower() or "debug" in text.lower() or "fix" in text.lower()):
        return True
    return False


def decide_route(
    *,
    cfg: RouterConfig,
    request_model: str,
    headers: Dict[str, str],
    messages: Optional[Iterable[Dict[str, Any]]] = None,
    has_tools: bool = False,
    enable_policy: bool = False,
    enable_request_type: bool = False,
) -> RouteDecision:
    """Select {backend, model} with simple, stable heuristics."""

    hdr_backend = _known_backend_name((headers.get("x-backend") or "").strip())
    if hdr_backend:
        normalized = _normalize_model(request_model, hdr_backend, cfg)
        return RouteDecision(backend=hdr_backend, model=normalized, reason="override:x-backend")

    request_model_norm = (request_model or "").strip()
    request_model_key = request_model_norm.lower()
    if request_model_key in {"auto"}:
        request_model_norm = ""
        request_model_key = ""

    aliases = get_aliases()

    alias_key = request_model_key
    if alias_key and alias_key in aliases:
        a = aliases[alias_key]
        backend = _resolved_backend_name(a.backend) or a.backend
        normalized = _normalize_model(a.upstream_model, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="alias:model")

    backend = _choose_backend_by_model(request_model_norm, cfg.default_backend)

    explicitly_pinned = _backend_from_model_prefix(request_model_norm) is not None
    if not explicitly_pinned:
        pinned_backend = _known_backend_name(request_model_norm)
        explicitly_pinned = pinned_backend is not None and request_model_key not in aliases

    if explicitly_pinned:
        normalized = _normalize_model(request_model_norm, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="pinned:model")

    if not enable_policy:
        normalized = _normalize_model(request_model_norm, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="direct:model")

    size = _approx_text_size(messages or [])

    long_alias = get_alias("long")
    long_threshold = int(long_alias.context_window) if (long_alias and long_alias.context_window) else cfg.long_context_chars_threshold

    provider = backend_provider_name(backend)

    if has_tools:
        a = get_alias("default")
        if a and a.tools is not False:
            b = _resolved_backend_name(a.backend) or a.backend
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:tools->alias:default")
        a = get_alias("coder")
        if a and a.tools is not False:
            b = _resolved_backend_name(a.backend) or a.backend
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:tools->alias:coder")
        return RouteDecision(backend=backend, model=cfg.primary_strong_model, reason="policy:tools->strong")

    if size >= long_threshold:
        a = get_alias("long")
        if a:
            b = _resolved_backend_name(a.backend) or a.backend
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:long_context->alias:long")
        primary_backend = _provider_default_backend("mlx")
        if cfg.primary_strong_model:
            return RouteDecision(backend=primary_backend, model=cfg.primary_strong_model, reason="policy:long_context->primary")
        return RouteDecision(backend=backend, model=cfg.primary_strong_model, reason="policy:long_context->strong")

    hdr_req_type = (headers.get("x-request-type") or "").strip().lower()
    is_coding = False
    if enable_request_type:
        if hdr_req_type in {"coding", "code", "dev"}:
            is_coding = True
        elif hdr_req_type in {"chat", "general"}:
            is_coding = False
        else:
            is_coding = _is_probably_coding_request(messages or [])

    if is_coding:
        a = get_alias("coder")
        if a:
            b = _resolved_backend_name(a.backend) or a.backend
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:coding->alias:coder")
        return RouteDecision(backend=backend, model=cfg.primary_strong_model, reason="policy:coding->strong")

    a = get_alias("fast")
    if a:
        b = _resolved_backend_name(a.backend) or a.backend
        return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:fast->alias:fast")

    return RouteDecision(backend=backend, model=cfg.primary_fast_model, reason="policy:fast")
