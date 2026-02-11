#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import ssl
from typing import Any

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_TLS_CONTEXT: ssl.SSLContext | None = None


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout_sec: float = 60.0,
    max_body_bytes: int = 20_000_000,
) -> tuple[int, dict[str, str], Any]:
    raw: bytes | None = None
    h = dict(headers or {})

    if body is not None:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        h.setdefault("content-type", "application/json")

    req = Request(url=url, data=raw, headers=h, method=method.upper())

    try:
        with urlopen(req, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            headers_out = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read(max_body_bytes + 1)[:max_body_bytes]
            parsed = json.loads(data.decode("utf-8")) if data else None
            return status, headers_out, parsed
    except HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        try:
            data = e.read(max_body_bytes + 1)[:max_body_bytes]
            parsed = json.loads(data.decode("utf-8")) if data else None
        except Exception:
            parsed = None
        return status, {k.lower(): v for k, v in getattr(e, "headers", {}).items()}, parsed
    except URLError as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def _check_a1111(a1111_base: str) -> None:
    base = a1111_base.rstrip("/")

    # A1111 exposes a small list of endpoints. This verifies connectivity and that --api is enabled.
    status, _h, models = _http_json("GET", f"{base}/sdapi/v1/sd-models", timeout_sec=20)
    if status != 200:
        raise RuntimeError(f"A1111 check failed: GET /sdapi/v1/sd-models status={status} body={models!r}")

    if not isinstance(models, list):
        raise RuntimeError(f"A1111 unexpected sd-models response: {type(models).__name__}")

    # Minimal generation; keeps it tiny and fast.
    payload = {
        "prompt": "gateway verify_images smoke test",
        "width": 256,
        "height": 256,
        "steps": 1,
        "batch_size": 1,
    }
    status, _h, out = _http_json("POST", f"{base}/sdapi/v1/txt2img", body=payload, timeout_sec=120)
    if status != 200:
        raise RuntimeError(f"A1111 check failed: POST /sdapi/v1/txt2img status={status} body={out!r}")

    images = out.get("images") if isinstance(out, dict) else None
    if not (isinstance(images, list) and images and isinstance(images[0], str) and len(images[0]) > 20):
        raise RuntimeError("A1111 txt2img response missing 'images' base64 strings")


def _check_openai_images(images_base: str, *, model: str) -> None:
    base = images_base.rstrip("/")
    payload = {
        "model": model,
        "prompt": "gateway verify_images smoke test",
        "size": "256x256",
        "n": 1,
        "response_format": "b64_json",
    }

    status, _h, out = _http_json("POST", f"{base}/v1/images/generations", body=payload, timeout_sec=120)
    if status != 200:
        raise RuntimeError(f"images server check failed: POST /v1/images/generations status={status} body={out!r}")

    data = out.get("data") if isinstance(out, dict) else None
    if not (isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("b64_json"), str)):
        raise RuntimeError("images server response missing data[0].b64_json")


def _check_gateway_images(gateway_base: str, token: str) -> None:
    base = gateway_base.rstrip("/")
    headers = {"authorization": f"Bearer {token}"}
    payload = {"prompt": "gateway verify_images smoke test", "size": "256x256", "n": 1, "response_format": "b64_json"}

    status, _h, out = _http_json("POST", f"{base}/v1/images/generations", headers=headers, body=payload, timeout_sec=120)
    if status != 200:
        raise RuntimeError(f"gateway check failed: POST /v1/images/generations status={status} body={out!r}")

    data = out.get("data") if isinstance(out, dict) else None
    if not (isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("b64_json"), str)):
        raise RuntimeError("gateway images response missing data[0].b64_json")


def _check_ui_image(gateway_base: str) -> None:
    base = gateway_base.rstrip("/")
    payload = {"prompt": "gateway verify_images ui smoke test", "size": "256x256", "n": 1, "response_format": "b64_json"}

    status, _h, out = _http_json("POST", f"{base}/ui/api/image", body=payload, timeout_sec=120)
    if status != 200:
        raise RuntimeError(f"ui check failed: POST /ui/api/image status={status} body={out!r}")

    data = out.get("data") if isinstance(out, dict) else None
    if not (isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("b64_json"), str)):
        raise RuntimeError("ui image response missing data[0].b64_json")


def _env_gateway_token() -> str:
    tok = (os.getenv("GATEWAY_BEARER_TOKEN") or "").strip()
    if tok:
        return tok
    raw = (os.getenv("GATEWAY_BEARER_TOKENS") or "").strip()
    if raw:
        for part in raw.split(","):
            t = part.strip()
            if t:
                return t
    return ""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Smoke-test A1111 and gateway image generation endpoints.")
    p.add_argument("--gateway-base-url", default="https://127.0.0.1:8800")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed local certs).",
    )
    p.add_argument(
        "--token",
        default="",
        help="Bearer token for /v1/images/generations (default: $GATEWAY_BEARER_TOKEN or first of $GATEWAY_BEARER_TOKENS).",
    )
    p.add_argument(
        "--also-check-a1111",
        default="",
        metavar="A1111_BASE_URL",
        help="If set, verify A1111 is reachable and txt2img works (e.g. http://127.0.0.1:7860)",
    )
    p.add_argument(
        "--also-check-openai-images",
        default="",
        metavar="IMAGES_BASE_URL",
        help="If set, verify an OpenAI-style image server is reachable (e.g. InvokeAI shim at http://ada2:7860)",
    )
    p.add_argument(
        "--openai-images-model",
        default="",
        help="Model id to use with --also-check-openai-images (e.g. NexaAI/sdxl-turbo)",
    )
    p.add_argument(
        "--check-ui",
        action="store_true",
        help="Also verify /ui/api/image (requires UI_IP_ALLOWLIST to allow this client).",
    )

    args = p.parse_args(argv)

    insecure = args.insecure or (os.getenv("GATEWAY_TLS_INSECURE") or "").strip().lower() in {"1", "true", "yes", "on"}
    global _TLS_CONTEXT
    if insecure:
        _TLS_CONTEXT = ssl._create_unverified_context()
    else:
        _TLS_CONTEXT = ssl.create_default_context()

    token = (args.token or "").strip() or _env_gateway_token()

    if args.also_check_a1111:
        print(f"[1/3] Checking A1111 at {args.also_check_a1111} ...")
        _check_a1111(args.also_check_a1111)
        print("OK: A1111 reachable, API enabled, txt2img succeeded")

    if args.also_check_openai_images:
        if not args.openai_images_model:
            raise RuntimeError("--openai-images-model is required when using --also-check-openai-images")
        print(f"[1/3] Checking OpenAI-style images server at {args.also_check_openai_images} ...")
        _check_openai_images(args.also_check_openai_images, model=args.openai_images_model)
        print("OK: images server reachable, generations returned b64_json")

    if not token:
        print("Skipping gateway /v1/images/generations check (no --token provided)")
    else:
        print(f"[2/3] Checking gateway images at {args.gateway_base_url} ...")
        _check_gateway_images(args.gateway_base_url, token)
        print("OK: gateway /v1/images/generations returned b64_json")

    if args.check_ui:
        print(f"[3/3] Checking tokenless UI image endpoint at {args.gateway_base_url} ...")
        _check_ui_image(args.gateway_base_url)
        print("OK: gateway /ui/api/image returned b64_json")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(2)
