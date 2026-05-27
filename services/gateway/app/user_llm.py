from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from app.httpx_client import httpx_client as _httpx_client
from app.models import ChatCompletionRequest
from app.openai_utils import new_id, now_unix, sanitize_chat_choices, sse, sse_done
from app.streaming import passthrough_sse
from app.upstreams import _normalize_messages_for_openai_backend


USER_LLM_MODEL_PREFIX = "user_llm:"

_PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "custom_openai": {
        "label": "Custom OpenAI-compatible",
        "base_url": "",
    },
}


async def _http_status_error_detail(exc: httpx.HTTPStatusError, *, upstream: str) -> Dict[str, Any]:
    response = exc.response
    body = ""
    if response is not None:
        try:
            body = response.text
        except httpx.ResponseNotRead:
            try:
                raw = await response.aread()
            except Exception:
                raw = b""
            body = raw.decode(response.encoding or "utf-8", errors="replace")
    return {
        "upstream": upstream,
        "status": response.status_code if response is not None else None,
        "body": body[:5000],
    }


def token_hint(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 10:
        return "***"
    return f"{raw[:4]}...{raw[-4:]}"


def normalize_provider_id(provider: str) -> str:
    value = str(provider or "").strip().lower().replace("-", "_")
    if value in {"custom", "openai_compatible", "openai_compat"}:
        return "custom_openai"
    return value


def supported_provider_ids() -> set[str]:
    return set(_PROVIDER_DEFAULTS.keys())


def provider_label(provider: str) -> str:
    pid = normalize_provider_id(provider)
    configured = _PROVIDER_DEFAULTS.get(pid)
    if configured:
        return configured["label"]
    return pid.replace("_", " ").title()


def default_base_url(provider: str) -> str:
    pid = normalize_provider_id(provider)
    return str((_PROVIDER_DEFAULTS.get(pid) or {}).get("base_url") or "").rstrip("/")


def user_model_id(provider: str, model: str) -> str:
    return f"{USER_LLM_MODEL_PREFIX}{normalize_provider_id(provider)}:{str(model or '').strip()}"


def parse_user_model_id(model_id: str) -> Optional[Tuple[str, str]]:
    raw = str(model_id or "").strip()
    if not raw.startswith(USER_LLM_MODEL_PREFIX):
        return None
    rest = raw[len(USER_LLM_MODEL_PREFIX) :]
    provider, sep, model = rest.partition(":")
    provider = normalize_provider_id(provider)
    model = str(model or "").strip()
    if not sep or not provider or not model:
        return None
    return provider, model


def is_user_model_id(model_id: str) -> bool:
    return parse_user_model_id(model_id) is not None


def _llm_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    raw = settings.get("commercial_llms") if isinstance(settings, dict) else None
    return raw if isinstance(raw, dict) else {}


def _providers(settings: Dict[str, Any]) -> Dict[str, Any]:
    llms = _llm_settings(settings)
    raw = llms.get("providers")
    return raw if isinstance(raw, dict) else {}


def _coerce_models(raw: Any) -> List[str]:
    values: List[str] = []
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
        values = [p.strip() for p in parts]
    elif isinstance(raw, list):
        values = [str(p or "").strip() for p in raw]
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _provider_config(settings: Dict[str, Any], provider: str) -> Dict[str, Any]:
    pid = normalize_provider_id(provider)
    raw = _providers(settings).get(pid)
    return raw if isinstance(raw, dict) else {}


def _provider_enabled(settings: Dict[str, Any], provider: str) -> bool:
    llms = _llm_settings(settings)
    if not bool(llms.get("enabled")):
        return False
    cfg = _provider_config(settings, provider)
    return bool(cfg.get("enabled"))


def _api_key(settings: Dict[str, Any], provider: str) -> str:
    cfg = _provider_config(settings, provider)
    return str(cfg.get("api_key") or "").strip()


def _base_url(settings: Dict[str, Any], provider: str) -> str:
    cfg = _provider_config(settings, provider)
    return str(cfg.get("base_url") or default_base_url(provider) or "").strip().rstrip("/")


def configured_models(settings: Dict[str, Any], provider: str) -> List[str]:
    return _coerce_models(_provider_config(settings, provider).get("models"))


def available_models(settings: Dict[str, Any], provider: str) -> List[str]:
    cfg = _provider_config(settings, provider)
    available = _coerce_models(cfg.get("available_models"))
    configured = configured_models(settings, provider)
    out: List[str] = []
    seen: set[str] = set()
    for model in [*available, *configured]:
        if not model or model in seen:
            continue
        seen.add(model)
        out.append(model)
    return out


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            text = _extract_text_from_content(item)
            if text:
                parts.append(text)
        return "".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content", "value", "delta", "output_text"):
            text = _extract_text_from_content(content.get(key))
            if text:
                return text
    return ""


def extract_assistant_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    top_level = _extract_text_from_content(payload.get("output_text"))
    if top_level:
        return top_level

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                text = _extract_text_from_content(message.get("content"))
                if text:
                    return text
            delta = choice.get("delta")
            if isinstance(delta, dict):
                text = _extract_text_from_content(delta.get("content"))
                if text:
                    return text

    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            text = _extract_text_from_content(item.get("content"))
            if text:
                return text

    message = payload.get("message")
    if isinstance(message, dict):
        return _extract_text_from_content(message.get("content"))

    return ""


def model_entries(settings: Dict[str, Any], *, created: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    llms = _llm_settings(settings)
    if not bool(llms.get("enabled")):
        return out
    for provider in sorted(_providers(settings).keys()):
        pid = normalize_provider_id(provider)
        if not _provider_enabled(settings, pid):
            continue
        if not _api_key(settings, pid):
            continue
        if not _base_url(settings, pid):
            continue
        for model in configured_models(settings, pid):
            mid = user_model_id(pid, model)
            out.append(
                {
                    "id": mid,
                    "object": "model",
                    "created": created,
                    "owned_by": pid,
                    "is_user_llm": True,
                    "provider": pid,
                    "upstream_model": model,
                    "label": f"{provider_label(pid)}: {model} (user API key)",
                }
            )
    return out


def resolve_user_model(settings: Dict[str, Any], model_id: str) -> Dict[str, str]:
    parsed = parse_user_model_id(model_id)
    if not parsed:
        raise HTTPException(status_code=400, detail={"error": "not_user_llm_model", "model": model_id})
    provider, model = parsed
    if not _provider_enabled(settings, provider):
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_provider_disabled", "provider": provider, "message": f"{provider_label(provider)} is not enabled in Settings"},
        )
    key = _api_key(settings, provider)
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_api_key_missing", "provider": provider, "message": f"{provider_label(provider)} API key is not configured in Settings"},
        )
    base_url = _base_url(settings, provider)
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_base_url_missing", "provider": provider, "message": f"{provider_label(provider)} base URL is not configured in Settings"},
        )
    configured = configured_models(settings, provider)
    if configured and model not in configured:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_model_not_configured", "provider": provider, "model": model},
        )
    return {"provider": provider, "model": model, "base_url": base_url, "api_key": key}


def user_backend_name(provider: str) -> str:
    return f"user_llm:{normalize_provider_id(provider)}"


def _payload_for_user_chat(req: ChatCompletionRequest, upstream_model: str, *, stream: Optional[bool] = None) -> Dict[str, Any]:
    payload = req.model_dump(exclude_none=True)
    payload["model"] = upstream_model
    if stream is not None:
        payload["stream"] = bool(stream)
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = _normalize_messages_for_openai_backend(payload["messages"])
    return payload


def _headers(provider: str, api_key: str, *, stream: bool = False) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    else:
        headers["Accept"] = "application/json"
    if normalize_provider_id(provider) == "openrouter":
        headers["X-Title"] = "Nexus"
    return headers


def extract_model_ids(payload: Any) -> List[str]:
    if isinstance(payload, dict):
        raw_items = payload.get("data")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return []

    out: List[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model_id = ""
        if isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
        elif isinstance(item, str):
            model_id = item.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
    return out


async def discover_provider_models(
    *,
    provider: str,
    settings: Dict[str, Any],
    api_key: str = "",
    base_url: str = "",
) -> List[str]:
    pid = normalize_provider_id(provider)
    if pid not in _PROVIDER_DEFAULTS:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_unknown_provider", "provider": pid, "message": "Unknown external LLM provider"},
        )
    key = str(api_key or "").strip() or _api_key(settings, pid)
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_api_key_missing", "provider": pid, "message": f"{provider_label(pid)} API key is required to load models"},
        )

    target_base = str(base_url or "").strip().rstrip("/") or _base_url(settings, pid)
    if not target_base:
        raise HTTPException(
            status_code=400,
            detail={"error": "user_llm_base_url_missing", "provider": pid, "message": f"{provider_label(pid)} base URL is required to load models"},
        )

    async with _httpx_client(timeout=30) as client:
        try:
            r = await client.get(f"{target_base}/models", headers=_headers(pid, key, stream=False))
            r.raise_for_status()
            models = extract_model_ids(r.json())
            if not models:
                raise HTTPException(
                    status_code=502,
                    detail={"upstream": user_backend_name(pid), "error": "No model IDs returned by provider /models endpoint"},
                )
            return models[:1000]
        except HTTPException:
            raise
        except httpx.HTTPStatusError as e:
            detail = await _http_status_error_detail(e, upstream=user_backend_name(pid))
            raise HTTPException(status_code=502, detail=detail) from e
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": user_backend_name(pid), "error": str(e)}) from e


async def call_user_chat(req: ChatCompletionRequest, *, model_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    target = resolve_user_model(settings, model_id)
    provider = target["provider"]
    upstream_model = target["model"]
    base_url = target["base_url"]
    payload = _payload_for_user_chat(req, upstream_model, stream=False)
    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=_headers(provider, target["api_key"], stream=False),
            )
            r.raise_for_status()
            out = r.json()
            if isinstance(out, dict):
                sanitize_chat_choices(out)
                out["model"] = model_id
            return out
        except httpx.HTTPStatusError as e:
            detail = await _http_status_error_detail(e, upstream=user_backend_name(provider))
            raise HTTPException(status_code=502, detail=detail) from e
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": user_backend_name(provider), "error": str(e)}) from e


async def stream_user_chat_as_openai(req: ChatCompletionRequest, *, model_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
    try:
        target = resolve_user_model(settings, model_id)
    except HTTPException as e:
        yield sse({"error": {"message": "User LLM configuration error", "type": "user_llm_error", "param": None, "code": None, "detail": e.detail}})
        yield sse_done()
        return

    provider = target["provider"]
    payload = _payload_for_user_chat(req, target["model"], stream=True)
    async with _httpx_client(timeout=None) as client:
        try:
            async with client.stream(
                "POST",
                f"{target['base_url']}/chat/completions",
                json=payload,
                headers=_headers(provider, target["api_key"], stream=True),
            ) as r:
                r.raise_for_status()
                content_type = (r.headers.get("content-type") or "").lower()
                if content_type and "text/event-stream" not in content_type:
                    raw = await r.aread()
                    try:
                        out = json.loads(raw.decode("utf-8", errors="replace"))
                    except Exception:
                        detail = {
                            "upstream": user_backend_name(provider),
                            "content_type": content_type,
                            "body": raw[:1000].decode("utf-8", errors="replace"),
                        }
                        yield sse(
                            {
                                "error": {
                                    "message": "Upstream returned a non-streaming response that was not valid JSON",
                                    "type": "upstream_error",
                                    "param": None,
                                    "code": None,
                                    "detail": detail,
                                }
                            }
                        )
                        yield sse_done()
                        return

                    if isinstance(out, dict) and isinstance(out.get("error"), (dict, str)):
                        yield sse(
                            {
                                "error": {
                                    "message": "Upstream error",
                                    "type": "upstream_error",
                                    "param": None,
                                    "code": None,
                                    "detail": {"upstream": user_backend_name(provider), "error": out.get("error")},
                                }
                            }
                        )
                        yield sse_done()
                        return

                    if isinstance(out, dict):
                        sanitize_chat_choices(out)
                        content = extract_assistant_text(out)
                        if isinstance(content, str) and content:
                            yield sse(
                                {
                                    "id": new_id("chatcmpl"),
                                    "object": "chat.completion.chunk",
                                    "created": now_unix(),
                                    "model": model_id,
                                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                                }
                            )
                            yield sse_done()
                            return

                    detail = {"upstream": user_backend_name(provider), "content_type": content_type}
                    yield sse(
                        {
                            "error": {
                                "message": "Upstream completed without assistant content",
                                "type": "empty_response",
                                "param": None,
                                "code": None,
                                "detail": detail,
                            }
                        }
                    )
                    yield sse_done()
                    return

                async for chunk in passthrough_sse(r):
                    yield chunk
        except httpx.HTTPStatusError as e:
            detail = await _http_status_error_detail(e, upstream=user_backend_name(provider))
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": user_backend_name(provider), "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()


def sanitize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    llms = settings.get("commercial_llms") if isinstance(settings, dict) else None
    if not isinstance(llms, dict):
        return settings
    providers = llms.get("providers")
    if not isinstance(providers, dict):
        return settings
    for raw_provider, raw_cfg in list(providers.items()):
        if not isinstance(raw_cfg, dict):
            continue
        token = str(raw_cfg.get("api_key") or "").strip()
        raw_cfg.pop("api_key", None)
        raw_cfg.pop("clear_api_key", None)
        raw_cfg["api_key_configured"] = bool(token)
        raw_cfg["api_key_hint"] = token_hint(token)
        pid = normalize_provider_id(str(raw_provider or ""))
        raw_cfg.setdefault("label", provider_label(pid))
        raw_cfg.setdefault("base_url", default_base_url(pid))
        raw_cfg["models"] = _coerce_models(raw_cfg.get("models"))
        raw_cfg["available_models"] = available_models({"commercial_llms": {"enabled": True, "providers": {pid: raw_cfg}}}, pid)
    return settings


def merge_settings_with_secrets(current: Dict[str, Any], patch: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
    patch_llms = patch.get("commercial_llms") if isinstance(patch, dict) else None
    if not isinstance(patch_llms, dict):
        return merged

    merged_llms = merged.get("commercial_llms")
    if not isinstance(merged_llms, dict):
        merged_llms = {}
        merged["commercial_llms"] = merged_llms
    merged_providers = merged_llms.get("providers")
    if not isinstance(merged_providers, dict):
        merged_providers = {}
        merged_llms["providers"] = merged_providers

    patch_providers = patch_llms.get("providers")
    if not isinstance(patch_providers, dict):
        return merged

    current_providers = _providers(current if isinstance(current, dict) else {})
    for raw_provider, patch_cfg in patch_providers.items():
        if not isinstance(patch_cfg, dict):
            continue
        provider = normalize_provider_id(str(raw_provider or ""))
        if not provider:
            continue
        merged_cfg = merged_providers.get(provider)
        if not isinstance(merged_cfg, dict):
            merged_cfg = {}
            merged_providers[provider] = merged_cfg
        current_cfg = current_providers.get(provider)
        current_key = str((current_cfg or {}).get("api_key") or "").strip() if isinstance(current_cfg, dict) else ""

        clear_key = bool(patch_cfg.get("clear_api_key"))
        key_supplied = "api_key" in patch_cfg
        supplied_key = str(patch_cfg.get("api_key") or "").strip() if key_supplied else ""
        if clear_key:
            merged_cfg.pop("api_key", None)
        elif key_supplied:
            if supplied_key:
                merged_cfg["api_key"] = supplied_key
            elif current_key:
                merged_cfg["api_key"] = current_key
            else:
                merged_cfg.pop("api_key", None)

        models = _coerce_models(merged_cfg.get("models"))
        if models:
            merged_cfg["models"] = models
        elif "models" in merged_cfg:
            merged_cfg["models"] = []

        available = _coerce_models(merged_cfg.get("available_models"))
        if available or models:
            merged_cfg["available_models"] = list(dict.fromkeys([*available, *models]))
        elif "available_models" in merged_cfg:
            merged_cfg["available_models"] = []

        if "base_url" in merged_cfg:
            merged_cfg["base_url"] = str(merged_cfg.get("base_url") or "").strip()
        merged_cfg.pop("clear_api_key", None)
        merged_cfg.pop("api_key_configured", None)
        merged_cfg.pop("api_key_hint", None)
        merged_cfg.pop("label", None)

    return merged
