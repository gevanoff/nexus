from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

import httpx
from fastapi import HTTPException

from app.backends import check_capability, get_admission_controller, get_registry
from app.config import S
from app.health_checker import check_backend_ready


def _backend_base_url(backend_class: str) -> str:
    registry = get_registry()
    backend = registry.get_backend(backend_class)
    if backend and isinstance(backend.base_url, str) and backend.base_url.strip():
        return backend.base_url.strip().rstrip("/")
    return ""


def resolve_ocr_backend_class(requested_backend_class: str = "") -> str:
    backend_class = (requested_backend_class or "").strip() or (getattr(S, "OCR_BACKEND_CLASS", "") or "").strip() or "lighton_ocr"
    registry = get_registry()
    cfg = registry.get_backend(backend_class)
    if not cfg or not cfg.supports("ocr"):
        raise HTTPException(status_code=400, detail={"error": "ocr_backend_unavailable", "backend_class": backend_class})
    return cfg.backend_class


def extract_ocr_text(payload: Any) -> str:
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        data = payload.get("data")
        if isinstance(data, list):
            parts: List[str] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                for key in ("text", "raw_text", "transcript", "generated_text"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                        break
                else:
                    lines = item.get("lines")
                    if isinstance(lines, list):
                        for line in lines:
                            if isinstance(line, dict) and isinstance(line.get("text"), str) and line["text"].strip():
                                parts.append(line["text"].strip())
            if parts:
                return "\n".join(parts)
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _collapse_repeated_lines(text: str) -> Tuple[str, str]:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    if len(nonempty) < 6:
        return text, ""

    counts: Dict[str, int] = {}
    for line in nonempty:
        counts[line] = counts.get(line, 0) + 1
    repeated_line, repeated_count = max(counts.items(), key=lambda item: item[1])
    if repeated_count < 4 or repeated_count / max(1, len(nonempty)) < 0.65:
        return text, ""

    collapsed: List[str] = []
    previous = ""
    for line in lines:
        normalized = line.strip()
        if normalized and normalized == previous:
            continue
        collapsed.append(line)
        if normalized:
            previous = normalized
        else:
            previous = ""
    warning = f"collapsed repeated OCR line {repeated_count} times: {repeated_line[:120]}"
    return "\n".join(collapsed).strip(), warning


def normalize_ocr_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    text = extract_ocr_text(payload)
    if not text:
        return payload
    collapsed, warning = _collapse_repeated_lines(text)
    if not warning:
        return payload

    out = dict(payload)
    out["text"] = collapsed
    if isinstance(out.get("data"), list) and out["data"] and isinstance(out["data"][0], dict):
        out["data"] = [dict(item) if isinstance(item, dict) else item for item in out["data"]]
        out["data"][0]["text"] = collapsed
    gateway = out.get("_gateway") if isinstance(out.get("_gateway"), dict) else {}
    gateway["ocr_warning"] = warning
    out["_gateway"] = gateway
    return out


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


async def _ocr_steering_retry(original_body: Dict[str, Any], payload: Dict[str, Any], warning: str) -> Dict[str, Any]:
    if not _bool_env("OCR_LLM_STEERING_ENABLED", True):
        return {}

    token = (os.getenv("GATEWAY_BEARER_TOKEN") or "").strip()
    if not token:
        return {"error": "missing GATEWAY_BEARER_TOKEN"}

    text = extract_ocr_text(payload)
    context = {
        "task": "ocr_retry_steering",
        "warning": warning,
        "ocr_text_preview": text[:1200],
        "request_has_prompt": isinstance(original_body.get("prompt") or original_body.get("text"), str),
        "allowed": {
            "retry": "boolean",
            "prompt": "short OCR instruction",
            "parameters": {
                "max_new_tokens": "integer 64..512",
                "do_sample": False,
                "repetition_penalty": "float 1.0..1.25"
            },
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You steer an OCR retry. If output is repetitive, empty, or clearly hallucinated, "
                "return compact JSON with retry=true and conservative OCR generation parameters. "
                "Otherwise retry=false. Return JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))[:2400]},
    ]
    model = (os.getenv("OCR_LLM_STEERING_MODEL") or "fast").strip()
    base_url = (os.getenv("OCR_LLM_STEERING_BASE_URL") or "http://127.0.0.1:8800/v1").strip().rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 256},
            )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:500]}"}
        data = resp.json()
        content = ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = str(message.get("content") or "")
        start = content.find("{")
        end = content.rfind("}")
        proposal = json.loads(content[start : end + 1] if start >= 0 and end > start else content)
        return proposal if isinstance(proposal, dict) else {"error": "non-object proposal"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _build_retry_body(original_body: Dict[str, Any], proposal: Dict[str, Any]) -> Dict[str, Any]:
    retry_body = dict(original_body)
    prompt = proposal.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        retry_body["prompt"] = prompt.strip()[:500]
    else:
        retry_body["prompt"] = (
            "Read the visible text exactly once. Do not invent formulas or repeat lines. "
            "If no readable text is present, return an empty string."
        )

    params = retry_body.get("parameters") if isinstance(retry_body.get("parameters"), dict) else {}
    params = dict(params)
    proposed_params = proposal.get("parameters") if isinstance(proposal.get("parameters"), dict) else {}
    max_tokens = proposed_params.get("max_new_tokens", 192)
    try:
        params["max_new_tokens"] = max(64, min(512, int(max_tokens)))
    except Exception:
        params["max_new_tokens"] = 192
    params["do_sample"] = False
    try:
        params["repetition_penalty"] = max(1.0, min(1.25, float(proposed_params.get("repetition_penalty", 1.12))))
    except Exception:
        params["repetition_penalty"] = 1.12
    retry_body["parameters"] = params
    return retry_body


async def scan_ocr(body: Dict[str, Any], *, backend_class: str = "") -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    if not any(isinstance(body.get(key), str) and body.get(key).strip() for key in ("image_url", "image")):
        raise HTTPException(status_code=400, detail="image_url or image is required")

    resolved_backend = resolve_ocr_backend_class(backend_class or str(body.get("backend_class") or body.get("backend") or ""))
    check_backend_ready(resolved_backend, route_kind="ocr")
    await check_capability(resolved_backend, "ocr")

    admission = get_admission_controller()
    await admission.acquire(resolved_backend, "ocr")
    try:
        base = _backend_base_url(resolved_backend) or (getattr(S, "LIGHTON_OCR_API_BASE_URL", "") or "").strip().rstrip("/")
        if not base:
            raise HTTPException(
                status_code=503,
                detail=f"{resolved_backend} is not configured. Set its base_url in gateway config or env.",
            )
        timeout_sec = float(getattr(S, "LIGHTON_OCR_TIMEOUT_SEC", 120) or 120)
        timeout = httpx.Timeout(connect=10.0, read=timeout_sec, write=10.0, pool=10.0)
        request_body = {k: v for k, v in body.items() if k not in {"backend", "backend_class"}}
        async def call_backend(payload: Dict[str, Any]) -> Dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base}/v1/ocr", json=payload)
            if resp.status_code >= 400:
                try:
                    detail: Any = resp.json()
                except Exception:
                    detail = resp.text
                raise HTTPException(status_code=resp.status_code, detail=detail)
            try:
                data = resp.json()
            except Exception:
                raise HTTPException(status_code=502, detail=resp.text)
            if not isinstance(data, dict):
                data = {"text": str(data), "raw": data}
            return normalize_ocr_payload(data)

        data = await call_backend(request_body)
        gateway_meta = data.get("_gateway") if isinstance(data.get("_gateway"), dict) else {}
        warning = str(gateway_meta.get("ocr_warning") or "").strip()
        if warning or not extract_ocr_text(data):
            proposal = await _ocr_steering_retry(request_body, data, warning or "empty OCR text")
            if proposal.get("retry") is True:
                retry_body = _build_retry_body(request_body, proposal)
                retry_data = await call_backend(retry_body)
                retry_meta = retry_data.get("_gateway") if isinstance(retry_data.get("_gateway"), dict) else {}
                retry_meta["ocr_steering"] = {
                    "used": True,
                    "model": (os.getenv("OCR_LLM_STEERING_MODEL") or "fast").strip(),
                    "reason": warning or "empty OCR text",
                    "proposal": {
                        "prompt": retry_body.get("prompt"),
                        "parameters": retry_body.get("parameters"),
                    },
                }
                retry_data["_gateway"] = retry_meta
                data = retry_data
            elif proposal:
                gateway_meta["ocr_steering"] = {
                    "used": False,
                    "reason": warning or "empty OCR text",
                    "proposal": proposal,
                }
                data["_gateway"] = gateway_meta
        gateway_meta = data.get("_gateway") if isinstance(data.get("_gateway"), dict) else {}
        gateway_meta.update({"backend_class": resolved_backend, "base_url": base})
        data["_gateway"] = gateway_meta
        return data
    finally:
        admission.release(resolved_backend, "ocr")
