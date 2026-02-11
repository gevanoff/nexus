#!/usr/bin/env python3
"""Generate a reproducible-ish release manifest for the local gateway appliance.

Stdlib-only by design: this should be runnable on a fresh host with just Python.

It captures:
- deployed gateway + ai-infra commit stamps (if present)
- gateway health and discovered models (via gateway HTTP)
- upstream/backend versions and model lists (best-effort)

This is intentionally best-effort: missing tools/endpoints should not crash the
script; missing fields are recorded as null/"unknown".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import shutil
import subprocess
import sys
import ssl
from urllib.parse import urlparse
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_TLS_CONTEXT: ssl.SSLContext | None = None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        return s or None
    except Exception:
        return None


def _run_cmd(argv: list[str], *, timeout_sec: float = 2.0) -> Tuple[int, str]:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_sec, check=False)
        out = (cp.stdout or "") + ("\n" + (cp.stderr or "") if cp.stderr else "")
        return int(cp.returncode), out.strip()
    except Exception as e:
        return 127, f"{type(e).__name__}: {e}"


def _http_json(method: str, url: str, *, headers: Optional[dict[str, str]] = None, timeout_sec: float = 5.0) -> Tuple[Optional[int], Any, Optional[str]]:
    req = Request(url=url, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            raw = resp.read()
        try:
            return status, json.loads(raw.decode("utf-8")), None
        except Exception:
            return status, None, "invalid json"
    except HTTPError as e:
        try:
            body = e.read()
            detail = body[:400].decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return int(e.code), None, f"HTTPError: {e.code} {detail}".strip()
    except URLError as e:
        return None, None, f"URLError: {e}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


def _http_post_json(url: str, payload: dict[str, Any], *, headers: Optional[dict[str, str]] = None, timeout_sec: float = 10.0) -> Tuple[Optional[int], Any, Optional[str]]:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = Request(url=url, data=body, method="POST", headers=hdrs)
    try:
        with urlopen(req, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            raw = resp.read()
        try:
            return status, json.loads(raw.decode("utf-8")), None
        except Exception:
            return status, None, "invalid json"
    except HTTPError as e:
        try:
            body2 = e.read()
            detail = body2[:400].decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return int(e.code), None, f"HTTPError: {e.code} {detail}".strip()
    except URLError as e:
        return None, None, f"URLError: {e}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


@dataclass
class BestEffortField:
    value: Any
    error: Optional[str] = None


def _best_effort(fn, *args, **kwargs) -> BestEffortField:
    try:
        return BestEffortField(value=fn(*args, **kwargs), error=None)
    except Exception as e:
        return BestEffortField(value=None, error=f"{type(e).__name__}: {e}")


    def _derive_obs_url(base_url: str) -> str:
        override = (os.getenv("GATEWAY_OBS_URL") or "").strip()
        if override:
            return override

        obs_port = (os.getenv("OBSERVABILITY_PORT") or "8801").strip()
        try:
            obs_port_int = int(obs_port)
        except Exception:
            obs_port_int = 8801

        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        return f"http://{host}:{obs_port_int}"


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


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _deployed_commit_stamps(app_dir: str) -> dict[str, Optional[str]]:
    return {
        "gateway_commit": _read_text(os.path.join(app_dir, "DEPLOYED_GATEWAY_COMMIT")),
        "gateway_ref": _read_text(os.path.join(app_dir, "DEPLOYED_GATEWAY_GIT_REF")),
        "ai_infra_commit": _read_text(os.path.join(app_dir, "DEPLOYED_AI_INFRA_COMMIT")),
    }


def _collect_upstream_versions(*, ollama_base_url: str, mlx_base_url: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ollama": {}, "mlx": {}}

    # Ollama: /api/version and /api/tags (models list)
    if ollama_base_url:
        st, j, err = _http_json("GET", ollama_base_url.rstrip("/") + "/api/version", timeout_sec=2.5)
        out["ollama"]["api_version_status"] = st
        out["ollama"]["api_version"] = j
        out["ollama"]["api_version_error"] = err

        st, j, err = _http_json("GET", ollama_base_url.rstrip("/") + "/api/tags", timeout_sec=5.0)
        out["ollama"]["api_tags_status"] = st
        out["ollama"]["api_tags"] = j
        out["ollama"]["api_tags_error"] = err

    # MLX server (OpenAI-ish): /models if present
    if mlx_base_url:
        st, j, err = _http_json("GET", mlx_base_url.rstrip("/") + "/models", timeout_sec=5.0)
        out["mlx"]["models_status"] = st
        out["mlx"]["models"] = j
        out["mlx"]["models_error"] = err

    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Freeze a gateway appliance release manifest (stdlib-only).")
    p.add_argument("--out", default="", help="Output JSON path (default: /var/lib/gateway/app/release_manifest.json).")
    p.add_argument(
        "--app-dir",
        default=os.getenv("GATEWAY_APP_DIR") or "/var/lib/gateway/app",
        help="Runtime app dir (default: /var/lib/gateway/app).",
    )
    p.add_argument(
        "--base-url",
        default=os.getenv("GATEWAY_BASE_URL") or "https://127.0.0.1:8800",
        help="Gateway base URL for /v1/* queries.",
    )
    p.add_argument(
        "--obs-url",
        default=os.getenv("GATEWAY_OBS_URL") or "",
        help="Observability base URL for /health and /health/upstreams (defaults to derive from base URL).",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed local certs).",
    )
    p.add_argument(
        "--token",
        default="",
        help="Bearer token for gateway queries (default: $GATEWAY_BEARER_TOKEN or first of $GATEWAY_BEARER_TOKENS).",
    )
    p.add_argument(
        "--ollama-base-url",
        default=os.getenv("OLLAMA_BASE_URL") or "",
        help="Optional direct Ollama base URL (for version + model tags).",
    )
    p.add_argument(
        "--mlx-base-url",
        default=os.getenv("MLX_BASE_URL") or "",
        help="Optional direct MLX base URL (for model list).",
    )

    ns = p.parse_args(argv)

    insecure = ns.insecure or (os.getenv("GATEWAY_TLS_INSECURE") or "").strip().lower() in {"1", "true", "yes", "on"}
    global _TLS_CONTEXT
    if insecure:
        _TLS_CONTEXT = ssl._create_unverified_context()
    else:
        _TLS_CONTEXT = ssl.create_default_context()

    ns.token = (ns.token or "").strip() or _env_gateway_token()

    app_dir = os.path.abspath(ns.app_dir)
    out_path = ns.out.strip() or os.path.join(app_dir, "release_manifest.json")

    created_iso = _now_iso()
    created_unix = int(time.time())

    bearer: dict[str, str] = {}
    if ns.token.strip():
        bearer["authorization"] = f"Bearer {ns.token.strip()}"

    obs_url = (ns.obs_url or "").strip() or _derive_obs_url(ns.base_url)

    # Gateway queries (best-effort)
    health = _best_effort(_http_json, "GET", obs_url.rstrip("/") + "/health", headers=bearer, timeout_sec=3.0)
    upstreams = _best_effort(
        _http_json,
        "GET",
        obs_url.rstrip("/") + "/health/upstreams",
        headers=bearer,
        timeout_sec=5.0,
    )
    models = _best_effort(_http_json, "GET", ns.base_url.rstrip("/") + "/v1/models", headers=bearer, timeout_sec=10.0)

    # CLI versions
    ollama_cli = None
    if shutil.which("ollama"):
        rc, out = _run_cmd(["ollama", "--version"], timeout_sec=2.0)
        ollama_cli = {"rc": rc, "output": out}

    # Deployed stamp files written by deploy.sh (best-effort)
    stamps = _deployed_commit_stamps(app_dir)

    # Optional direct upstream inspection
    upstream_details = _best_effort(
        _collect_upstream_versions,
        ollama_base_url=ns.ollama_base_url.strip(),
        mlx_base_url=ns.mlx_base_url.strip(),
    )

    manifest: Dict[str, Any] = {
        "schema": "gateway_appliance_release_manifest.v1",
        "created_iso": created_iso,
        "created_unix": created_unix,
        "host": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version,
        },
        "runtime": {
            "app_dir": app_dir,
            "base_url": ns.base_url,
        },
        "source": {
            "gateway_commit": stamps.get("gateway_commit") or "unknown",
            "gateway_ref": stamps.get("gateway_ref") or None,
            "ai_infra_commit": stamps.get("ai_infra_commit") or "unknown",
        },
        "gateway": {
            "health": {"value": health.value, "error": health.error},
            "health_upstreams": {"value": upstreams.value, "error": upstreams.error},
            "v1_models": {"value": models.value, "error": models.error},
        },
        "backends": {
            "ollama_cli": ollama_cli,
            "ollama_base_url": ns.ollama_base_url or None,
            "mlx_base_url": ns.mlx_base_url or None,
            "upstream_details": {"value": upstream_details.value, "error": upstream_details.error},
        },
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
