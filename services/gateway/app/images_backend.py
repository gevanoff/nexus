from __future__ import annotations

import base64
import time
import re
from typing import Any, Dict, List, Literal, Tuple

import httpx

from app.config import S
from app.httpx_client import httpx_client as _httpx_client
from app.image_storage import convert_response_to_urls


def _effective_images_http_base_url() -> str:
    """Return the base URL for the configured images HTTP backend.

    Prefer explicit IMAGES_HTTP_BASE_URL, but if it appears to be the loopback default
    and an IMAGES_BACKEND_CLASS is configured in backends_config.yaml, derive from that
    backend's base_url to avoid config drift.
    """

    raw = (getattr(S, "IMAGES_HTTP_BASE_URL", "") or "").strip().rstrip("/")
    if not raw:
        return ""

    # If user explicitly configured a non-loopback URL, honor it.
    loopbacks = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
    if not any(raw.startswith(p) for p in loopbacks):
        return raw

    backend_class = (getattr(S, "IMAGES_BACKEND_CLASS", "") or "").strip() or "gpu_heavy"
    try:
        from app.backends import get_registry

        reg = get_registry()
        cfg = reg.get_backend(backend_class)
        if cfg and isinstance(cfg.base_url, str) and cfg.base_url.strip():
            derived = cfg.base_url.strip().rstrip("/")
            if derived and not any(derived.startswith(p) for p in loopbacks):
                return derived
    except Exception:
        pass

    return raw


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
        base = _effective_images_http_base_url()
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
            r.raise_for_status()
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
        base = _effective_images_http_base_url()
        if not base:
            raise RuntimeError("IMAGES_HTTP_BASE_URL is required for http_openai_images")

        timeout = float(getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
        chosen_model, model_reason = _select_images_model(prompt=prompt, requested_model=model)

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "n": n,
            "size": f"{width}x{height}",
            "response_format": "b64_json",
        }

        # Some OpenAI-ish image servers require `model`, but InvokeAI's shim can use a
        # configured default model. Only send the field when we have one.
        if chosen_model:
            payload["model"] = chosen_model

        # Only include extra knobs if explicitly provided by the caller.
        filtered = _filtered_options(options)
        payload.update(filtered)

        # Add a few alias fields for compatibility with various OpenAI-ish image servers.
        _apply_compat_aliases(payload, filtered)

        # SDXL-Turbo guidance: many implementations expect guidance to be disabled.
        # Apply a safe default only when the model name indicates turbo and the caller
        # did not already specify any guidance/cfg.
        guidance_used: float | None = None
        guidance_auto: bool = False
        if chosen_model and ("turbo" in chosen_model.lower()) and not _has_guidance(filtered):
            payload["guidance_scale"] = 0.0
            payload.setdefault("cfg_scale", 0.0)
            guidance_used = 0.0
            guidance_auto = True
        else:
            # If caller provided a value, surface the most specific one we can.
            for k in ("guidance_scale", "cfg_scale", "guidance"):
                if k in filtered:
                    try:
                        guidance_used = float(filtered[k])
                    except Exception:
                        guidance_used = None
                    break

        async with _httpx_client(timeout=timeout) as client:
            r = await client.post(f"{base}/v1/images/generations", json=payload)
            r.raise_for_status()
            out = r.json()

        data = out.get("data") if isinstance(out, dict) else None
        if not (isinstance(data, list) and data and all(isinstance(x, dict) for x in data)):
            raise RuntimeError("unexpected response from image backend")

        # Normalize to OpenAI-ish shape with b64_json.
        normalized: List[Dict[str, Any]] = []
        for item in data[:n]:
            b64 = item.get("b64_json")
            if isinstance(b64, str) and b64:
                normalized.append({"b64_json": b64})
        if not normalized:
            raise RuntimeError("image backend did not return b64_json")

        resp2: Dict[str, Any] = {"created": int(out.get("created") or time.time()), "data": normalized}
        resp2["_gateway"] = {"backend": backend, "mime": "image/png"}
        if chosen_model:
            resp2["_gateway"].update({"model": chosen_model, "model_reason": model_reason})
        if guidance_used is not None:
            resp2["_gateway"].update({"guidance_scale": guidance_used, "guidance_auto": guidance_auto})

        # Debug transparency: include the effective knobs we asked for.
        req_info: Dict[str, Any] = {
            "size": f"{width}x{height}",
        }
        for k in ("steps", "num_inference_steps", "seed", "negative_prompt", "negative"):
            if k in payload:
                req_info[k] = payload.get(k)
        if "guidance_scale" in payload:
            req_info["guidance_scale"] = payload.get("guidance_scale")
        if "cfg_scale" in payload:
            req_info["cfg_scale"] = payload.get("cfg_scale")

        resp2["_gateway"].update({"request": req_info})

        upstream_meta = _extract_upstream_meta(out)
        if upstream_meta:
            resp2["_gateway"].update({"upstream": upstream_meta})
        
        # Enforce response format policy
        if response_format == "url":
            resp2 = convert_response_to_urls(resp2)
        
        return resp2

    # Default: mock
    svg_bytes = _mock_svg(prompt, width, height)
    data = [{"b64_json": _b64(svg_bytes)} for _ in range(n)]
    resp = {"created": int(time.time()), "data": data, "_gateway": {"backend": "mock", "mime": "image/svg+xml"}}
    
    # Enforce response format policy
    if response_format == "url":
        resp = convert_response_to_urls(resp)
    
    return resp
