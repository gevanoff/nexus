from __future__ import annotations

import base64
import time
import re
from typing import Any, Dict, List, Literal, Tuple

import httpx

from app.config import S
from app.httpx_client import httpx_client as _httpx_client
from app.image_storage import convert_response_to_urls


def _get_image_backend_base_url(backend_class: str | None) -> str:
    backend_class = (backend_class or "").strip()
    if not backend_class:
        return ""
    try:
        from app.backends import get_registry

        reg = get_registry()
        cfg = reg.get_backend(backend_class)
        if cfg and isinstance(cfg.base_url, str):
            return cfg.base_url.strip().rstrip("/")
    except Exception:
        pass
    return ""


def resolve_images_backend_class(*, prompt: str = "", requested_model: str | None = None) -> str:
    requested = (requested_model or "").strip()
    if requested and requested.lower() != "auto":
        base = _get_image_backend_base_url(requested)
        if base:
            return requested

    enable_rt = bool(getattr(S, "IMAGES_ENABLE_REQUEST_TYPE", False))
    configured = (getattr(S, "IMAGES_BACKEND_CLASS", "") or "").strip() or "gpu_heavy"
    if not enable_rt or (requested and requested.lower() != "auto"):
        return configured

    fast = (getattr(S, "IMAGES_OPENAI_MODEL_FAST", "gpu_fast") or "gpu_fast").strip()
    slow = (getattr(S, "IMAGES_OPENAI_MODEL_SLOW", "gpu_heavy") or "gpu_heavy").strip()
    p = (prompt or "").strip()
    if _PHOTO_REAL_RE.search(p):
        return slow
    return fast


def _effective_images_http_base_url(backend_class: str | None = None) -> str:
    """Return the base URL for the configured images HTTP backend.

    Prefer explicit IMAGES_HTTP_BASE_URL, but if it appears to be the loopback default
    and an IMAGES_BACKEND_CLASS is configured in backends_config.yaml, derive from that
    backend's base_url to avoid config drift.
    """

    raw = (getattr(S, "IMAGES_HTTP_BASE_URL", "") or "").strip().rstrip("/")
    images_backend = (getattr(S, "IMAGES_BACKEND", "") or "mock").strip().lower()
    if images_backend in {"local_mlx", "mlx"}:
        images_backend = "http_openai_images"
    resolved_backend_class = (backend_class or "").strip() or (getattr(S, "IMAGES_BACKEND_CLASS", "") or "").strip() or "gpu_heavy"

    registry_base = _get_image_backend_base_url(resolved_backend_class)
    if registry_base and images_backend == "http_openai_images":
        return registry_base

    # For OpenAI-style image backend flow in Nexus, prefer the in-network images service.
    # This avoids deriving unrelated backend hostnames and failing with DNS ConnectError.
    if not raw and images_backend == "http_openai_images":
        return "http://images:7860"

    if not raw:
        return ""

    # If user explicitly configured a non-loopback URL, honor it.
    loopbacks = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
    if not any(raw.startswith(p) for p in loopbacks):
        return raw

    if images_backend == "http_openai_images":
        return "http://images:7860"

    try:
        if registry_base and not any(registry_base.startswith(p) for p in loopbacks):
            return registry_base
    except Exception:
        pass

    return raw


def _openai_images_endpoint(base_url: str, path: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    suffix = "/" + (path or "").strip().lstrip("/")
    if not base:
        return suffix
    if base.endswith("/v1"):
        return f"{base}{suffix}"
    return f"{base}/v1{suffix}"


def _parse_size(size: str) -> Tuple[int, int]:
    s = (size or "").strip().lower()
    if not s:
        s = "1024x1024"
    if "x" not in s:
        raise ValueError("size must be like '1024x1024'")
    a, b = s.split("x", 1)
    w = int(a.strip())
    h = int(b.strip())
    if w <= 0 or h <= 0:
        raise ValueError("size must be positive")
    max_px = int(getattr(S, "IMAGES_MAX_PIXELS", 2_000_000) or 2_000_000)
    if w * h > max_px:
        raise ValueError("size too large")
    return w, h


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


_PHOTO_REAL_RE = re.compile(
    r"\b(photoreal|photo-real|realistic|dslr|cinematic|35mm|bokeh|portrait photo|product photo|studio lighting|skin texture)\b",
    re.IGNORECASE,
)


def _select_images_model(*, prompt: str, requested_model: str | None) -> Tuple[str, str]:
    """Return (chosen_model, reason).

    Never returns the sentinel model name "auto".
    """

    rm = (requested_model or "").strip()
    cfg_model = (getattr(S, "IMAGES_OPENAI_MODEL", "") or "").strip()
    enable_rt = bool(getattr(S, "IMAGES_ENABLE_REQUEST_TYPE", False))

    # Explicit model always wins unless it's the sentinel "auto".
    if rm and rm.lower() != "auto":
        return rm, "request:model"

    # Configured model wins unless it's "auto".
    if cfg_model and cfg_model.lower() != "auto":
        return cfg_model, "config:model"

    # If request-type routing isn't enabled, omit model rather than forwarding "auto".
    if not enable_rt:
        return "", "omit:model"

    # Request-type: photoreal vs fast. Defaults are shim preset ids.
    fast = (getattr(S, "IMAGES_OPENAI_MODEL_FAST", "gpu_fast") or "gpu_fast").strip()
    slow = (getattr(S, "IMAGES_OPENAI_MODEL_SLOW", "gpu_slow") or "gpu_slow").strip()

    p = (prompt or "").strip()
    if _PHOTO_REAL_RE.search(p):
        return slow, "policy:images->slow"
    return fast, "policy:images->fast"


def _filter_image_options_for_upstream(opts: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(opts, dict) or not opts:
        return {}

    allowed = {
        "seed",
        "steps",
        "num_inference_steps",
        "guidance",
        "guidance_scale",
        "cfg_scale",
        "negative_prompt",
        "negative",
        "sampler",
        "scheduler",
        "style",
        "quality",
        "background",
        "output_format",
    }

    out: Dict[str, Any] = {}
    for k, v in opts.items():
        if k not in allowed or v is None:
            continue
        if isinstance(v, str):
            vv = v.strip()
            if not vv:
                continue
            out[k] = vv
            continue
        out[k] = v
    return out


def _apply_image_compat_aliases(payload: Dict[str, Any], filtered: Dict[str, Any]) -> None:
    if "steps" in filtered and "num_inference_steps" not in filtered:
        payload.setdefault("num_inference_steps", filtered.get("steps"))
    if "num_inference_steps" in filtered and "steps" not in filtered:
        payload.setdefault("steps", filtered.get("num_inference_steps"))

    if "negative_prompt" in filtered and "negative" not in filtered:
        payload.setdefault("negative", filtered.get("negative_prompt"))
    if "negative" in filtered and "negative_prompt" not in filtered:
        payload.setdefault("negative_prompt", filtered.get("negative"))

    if "guidance_scale" in payload and "cfg_scale" not in payload:
        payload.setdefault("cfg_scale", payload.get("guidance_scale"))
    if "cfg_scale" in payload and "guidance_scale" not in payload:
        payload.setdefault("guidance_scale", payload.get("cfg_scale"))


def _extract_image_upstream_meta(out: Any) -> Dict[str, Any]:
    if not isinstance(out, dict):
        return {}

    wanted = {
        "seed",
        "steps",
        "num_inference_steps",
        "guidance_scale",
        "cfg_scale",
        "sampler",
        "scheduler",
        "model",
    }

    def pull(d: Any) -> Dict[str, Any]:
        if not isinstance(d, dict):
            return {}
        meta: Dict[str, Any] = {}
        for k in wanted:
            if k not in d:
                continue
            v = d.get(k)
            if isinstance(v, (str, int, float, bool)):
                meta[k] = v
        return meta

    meta: Dict[str, Any] = {}
    meta.update(pull(out))
    for container_key in ("meta", "metadata", "parameters", "params", "info"):
        meta.update(pull(out.get(container_key)))
    return meta


def _has_image_guidance(opts: Dict[str, Any] | None) -> bool:
    if not isinstance(opts, dict) or not opts:
        return False
    for k in ("guidance", "guidance_scale", "cfg_scale"):
        if k in opts and opts.get(k) is not None:
            return True
    return False


def _normalize_openai_image_response(
    *,
    out: Dict[str, Any],
    response_format: str,
    n: int,
    backend_label: str,
    backend_class: str,
    base_url: str,
    mime: str = "image/png",
    chosen_model: str = "",
    model_reason: str = "",
    guidance_used: float | None = None,
    guidance_auto: bool = False,
    request_info: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    data = out.get("data") if isinstance(out, dict) else None
    if not (isinstance(data, list) and data and all(isinstance(x, dict) for x in data)):
        raise RuntimeError("unexpected response from image backend")

    normalized: List[Dict[str, Any]] = []
    for item in data[:n]:
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            normalized.append({"b64_json": b64})
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            normalized.append({"url": url})

    if not normalized:
        raise RuntimeError("image backend did not return image data")

    resp: Dict[str, Any] = {"created": int(out.get("created") or time.time()), "data": normalized}
    resp["_gateway"] = {
        "backend": backend_label,
        "backend_class": backend_class,
        "base_url": base_url,
        "mime": mime,
    }
    if chosen_model:
        resp["_gateway"].update({"model": chosen_model, "model_reason": model_reason})
    if guidance_used is not None:
        resp["_gateway"].update({"guidance_scale": guidance_used, "guidance_auto": guidance_auto})
    if request_info:
        resp["_gateway"].update({"request": request_info})

    upstream_meta = _extract_image_upstream_meta(out)
    if upstream_meta:
        resp["_gateway"].update({"upstream": upstream_meta})

    if response_format == "url":
        resp = convert_response_to_urls(resp)
    return resp


async def generate_openai_images(
    *,
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    model: str | None = None,
    options: Dict[str, Any] | None = None,
    response_format: str = "url",
    base_url: str,
    backend_label: str,
    backend_class: str,
    timeout_sec: float | None = None,
) -> Dict[str, Any]:
    n = int(n or 1)
    n = max(1, min(n, 4))
    width, height = _parse_size(size)
    timeout = float(timeout_sec or getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
    chosen_model, model_reason = _select_images_model(prompt=prompt, requested_model=model)

    payload: Dict[str, Any] = {
        "prompt": prompt,
        "n": n,
        "size": f"{width}x{height}",
        "response_format": "b64_json",
    }
    if chosen_model:
        payload["model"] = chosen_model

    filtered = _filter_image_options_for_upstream(options)
    payload.update(filtered)
    _apply_image_compat_aliases(payload, filtered)

    guidance_used: float | None = None
    guidance_auto = False
    if chosen_model and ("turbo" in chosen_model.lower()) and not _has_image_guidance(filtered):
        payload["guidance_scale"] = 0.0
        payload.setdefault("cfg_scale", 0.0)
        guidance_used = 0.0
        guidance_auto = True
    else:
        for k in ("guidance_scale", "cfg_scale", "guidance"):
            if k in filtered:
                try:
                    guidance_used = float(filtered[k])
                except Exception:
                    guidance_used = None
                break

    async with _httpx_client(timeout=timeout) as client:
        r = await client.post(_openai_images_endpoint(base_url, "/images/generations"), json=payload)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"image backend HTTP {r.status_code}: {detail}")
        out = r.json()

    request_info: Dict[str, Any] = {"size": f"{width}x{height}"}
    for k in ("steps", "num_inference_steps", "seed", "negative_prompt", "negative", "background", "output_format"):
        if k in payload:
            request_info[k] = payload.get(k)
    if "guidance_scale" in payload:
        request_info["guidance_scale"] = payload.get("guidance_scale")
    if "cfg_scale" in payload:
        request_info["cfg_scale"] = payload.get("cfg_scale")

    return _normalize_openai_image_response(
        out=out,
        response_format=response_format,
        n=n,
        backend_label=backend_label,
        backend_class=backend_class,
        base_url=base_url,
        chosen_model=chosen_model,
        model_reason=model_reason,
        guidance_used=guidance_used,
        guidance_auto=guidance_auto,
        request_info=request_info,
    )


async def edit_openai_images(
    *,
    prompt: str,
    image: tuple[str, bytes, str],
    mask: tuple[str, bytes, str] | None = None,
    form_fields: Dict[str, Any] | None = None,
    response_format: str = "url",
    base_url: str,
    backend_label: str,
    backend_class: str,
    timeout_sec: float | None = None,
) -> Dict[str, Any]:
    timeout = float(timeout_sec or getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
    data: Dict[str, Any] = {"prompt": prompt, "response_format": "b64_json"}
    if isinstance(form_fields, dict):
        for k, v in form_fields.items():
            if v is None:
                continue
            data[str(k)] = v

    files = {
        "image": image,
    }
    if mask is not None:
        files["mask"] = mask

    async with _httpx_client(timeout=timeout) as client:
        r = await client.post(_openai_images_endpoint(base_url, "/images/edits"), data=data, files=files)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"image edit backend HTTP {r.status_code}: {detail}")
        out = r.json()

    try:
        n = int(data.get("n") or 1)
    except Exception:
        n = 1

    return _normalize_openai_image_response(
        out=out,
        response_format=response_format,
        n=max(1, min(n, 4)),
        backend_label=backend_label,
        backend_class=backend_class,
        base_url=base_url,
        request_info={k: v for k, v in data.items() if k not in {"response_format"}},
    )


def _mock_svg(prompt: str, width: int, height: int) -> bytes:
    # Minimal placeholder image: preserves negative space (no crop) and is deterministic.
    p = (prompt or "").strip()
    if len(p) > 400:
        p = p[:400] + "…"

    # Escape basic XML characters.
    p = p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    svg = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\">
  <rect width=\"100%\" height=\"100%\" fill=\"#0b0d10\"/>
  <rect x=\"24\" y=\"24\" width=\"{max(0, width - 48)}\" height=\"{max(0, height - 48)}\" fill=\"#0e1217\" stroke=\"rgba(231,237,246,0.18)\"/>
  <text x=\"48\" y=\"72\" fill=\"#e7edf6\" font-family=\"ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial\" font-size=\"20\" font-weight=\"600\">Mock image backend</text>
  <text x=\"48\" y=\"104\" fill=\"#a9b4c3\" font-family=\"ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial\" font-size=\"14\">No image engine configured. Set IMAGES_BACKEND to http_a1111 to use a real generator.</text>
  <foreignObject x=\"48\" y=\"132\" width=\"{max(0, width - 96)}\" height=\"{max(0, height - 180)}\">
    <div xmlns=\"http://www.w3.org/1999/xhtml\" style=\"color:#c1ccdb;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;font-size:14px;line-height:1.5;white-space:pre-wrap;\">{p}</div>
  </foreignObject>
</svg>
"""
    return svg.encode("utf-8")


async def generate_images(
    *,
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    model: str | None = None,
    options: Dict[str, Any] | None = None,
    response_format: str = "url",
    backend_class: str | None = None,
) -> Dict[str, Any]:
    """Generate images in an OpenAI-ish response shape.

        Backends:
            - mock: returns a placeholder SVG (always available)
            - http_a1111: proxies to an Automatic1111-compatible API (txt2img)
            - http_openai_images: proxies to an OpenAI-style images server (POST /v1/images/generations)

        Response format:
            - url: Return URLs to stored images (default, enforces payload policy)
            - b64_json: Return base64-encoded images (only when explicitly requested)
    """

    # Normalize response_format
    response_format = (response_format or "url").strip().lower()
    if response_format not in {"url", "b64_json"}:
        raise ValueError(f"response_format must be 'url' or 'b64_json', got: {response_format}")

    n = int(n or 1)
    n = max(1, min(n, 4))
    width, height = _parse_size(size)

    backend: str = (getattr(S, "IMAGES_BACKEND", "mock") or "mock").strip().lower()
    if backend in {"local_mlx", "mlx"}:
        backend = "http_openai_images"

    def _filtered_options(opts: Dict[str, Any] | None) -> Dict[str, Any]:
        if not isinstance(opts, dict) or not opts:
            return {}

        # Conservative allowlist: upstream servers vary widely.
        allowed = {
            # Common knobs across SD/SDXL style servers.
            "seed",
            "steps",
            "num_inference_steps",
            "guidance",
            "guidance_scale",
            "cfg_scale",
            "negative_prompt",
            "negative",
            "sampler",
            "scheduler",
            "style",
            "quality",
        }

        out: Dict[str, Any] = {}
        for k, v in opts.items():
            if k not in allowed:
                continue
            if v is None:
                continue
            if isinstance(v, str):
                vv = v.strip()
                if not vv:
                    continue
                out[k] = vv
                continue
            out[k] = v

        return out

    def _apply_compat_aliases(payload: Dict[str, Any], filtered: Dict[str, Any]) -> None:
        # Some "OpenAI-ish" image servers accept SD-style fields but use different names.
        # Add harmless aliases when they are missing (best-effort).
        if "steps" in filtered and "num_inference_steps" not in filtered:
            payload.setdefault("num_inference_steps", filtered.get("steps"))
        if "num_inference_steps" in filtered and "steps" not in filtered:
            payload.setdefault("steps", filtered.get("num_inference_steps"))

        if "negative_prompt" in filtered and "negative" not in filtered:
            payload.setdefault("negative", filtered.get("negative_prompt"))
        if "negative" in filtered and "negative_prompt" not in filtered:
            payload.setdefault("negative_prompt", filtered.get("negative"))

        if "guidance_scale" in payload and "cfg_scale" not in payload:
            payload.setdefault("cfg_scale", payload.get("guidance_scale"))
        if "cfg_scale" in payload and "guidance_scale" not in payload:
            payload.setdefault("guidance_scale", payload.get("cfg_scale"))

    def _extract_upstream_meta(out: Any) -> Dict[str, Any]:
        # Best-effort: different servers return these in different places.
        # Keep it small and only surface a few well-known keys.
        if not isinstance(out, dict):
            return {}

        wanted = {
            "seed",
            "steps",
            "num_inference_steps",
            "guidance_scale",
            "cfg_scale",
            "sampler",
            "scheduler",
            "model",
        }

        def pull(d: Any) -> Dict[str, Any]:
            if not isinstance(d, dict):
                return {}
            m: Dict[str, Any] = {}
            for k in wanted:
                if k in d:
                    v = d.get(k)
                    if v is None:
                        continue
                    # Keep strings/nums/bools only; avoid dumping huge objects.
                    if isinstance(v, (str, int, float, bool)):
                        m[k] = v
            return m

        meta: Dict[str, Any] = {}
        meta.update(pull(out))
        for container_key in ("meta", "metadata", "parameters", "params", "info"):
            meta.update(pull(out.get(container_key)))

        return meta

    def _has_guidance(opts: Dict[str, Any] | None) -> bool:
        if not isinstance(opts, dict) or not opts:
            return False
        for k in ("guidance", "guidance_scale", "cfg_scale"):
            if k in opts and opts.get(k) is not None:
                return True
        return False

    if backend == "http_a1111":
        resolved_backend_class = (backend_class or "").strip() or resolve_images_backend_class(prompt=prompt, requested_model=model)
        base = _effective_images_http_base_url(resolved_backend_class)
        if not base:
            raise RuntimeError("IMAGES_HTTP_BASE_URL is required for http_a1111")

        timeout = float(getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
        payload = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "batch_size": n,
            # Keep defaults conservative; user can tune on the server.
            "steps": int(getattr(S, "IMAGES_A1111_STEPS", 20) or 20),
        }

        payload.update(_filtered_options(options))

        async with _httpx_client(timeout=timeout) as client:
            r = await client.post(f"{base}/sdapi/v1/txt2img", json=payload)
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text
                raise RuntimeError(f"image backend HTTP {r.status_code}: {detail}")
            out = r.json()

        images = out.get("images") if isinstance(out, dict) else None
        if not (isinstance(images, list) and images and all(isinstance(x, str) for x in images)):
            raise RuntimeError("unexpected response from image backend")

        data = [{"b64_json": images[i]} for i in range(min(n, len(images)))]
        resp: Dict[str, Any] = {"created": int(time.time()), "data": data}
        resp["_gateway"] = {"backend": backend, "mime": "image/png"}

        # Enforce response format policy
        if response_format == "url":
            resp = convert_response_to_urls(resp)

        return resp

    if backend == "http_openai_images":
        resolved_backend_class = (backend_class or "").strip() or resolve_images_backend_class(prompt=prompt, requested_model=model)
        base = _effective_images_http_base_url(resolved_backend_class)
        if not base:
            raise RuntimeError("IMAGES_HTTP_BASE_URL is required for http_openai_images")
        fallback_base = "http://images:7860"
        try:
            return await generate_openai_images(
                prompt=prompt,
                size=f"{width}x{height}",
                n=n,
                model=model,
                options=options,
                response_format=response_format,
                base_url=base,
                backend_label=backend,
                backend_class=resolved_backend_class,
            )
        except httpx.ConnectError as e:
            if base == fallback_base:
                raise RuntimeError(f"image backend connect error: {type(e).__name__}: {e}") from e
            return await generate_openai_images(
                prompt=prompt,
                size=f"{width}x{height}",
                n=n,
                model=model,
                options=options,
                response_format=response_format,
                base_url=fallback_base,
                backend_label=backend,
                backend_class=resolved_backend_class,
            )

    # Default: mock
    svg_bytes = _mock_svg(prompt, width, height)
    data = [{"b64_json": _b64(svg_bytes)} for _ in range(n)]
    resp = {"created": int(time.time()), "data": data, "_gateway": {"backend": "mock", "mime": "image/svg+xml"}}

    # Enforce response format policy
    if response_format == "url":
        resp = convert_response_to_urls(resp)

    return resp
