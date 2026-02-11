#!/usr/bin/env python3
import json
import os
import sys
from typing import Optional
import urllib.error
import urllib.request


DEFAULT_FYC_ENV_FILE = "/var/lib/followyourcanvas/followyourcanvas.env"
DEFAULT_GATEWAY_ENV_FILE = "/var/lib/gateway/app/.env"


def _load_env_file(path: str, *, prefix: Optional[str] = None) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if prefix and not key.startswith(prefix):
                    continue
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"')
    except OSError:
        return


def _read_input() -> dict:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def main() -> int:
    # Prefer explicit env vars (inherited from the gateway process), but allow
    # loading FYC_* settings from common env files.
    env_file_override = os.environ.get("FYC_ENV_FILE", "").strip()
    if env_file_override:
        _load_env_file(env_file_override, prefix="FYC_")

    # When the gateway is on a different host than the FollowYourCanvas service,
    # /var/lib/followyourcanvas/followyourcanvas.env typically won't exist on the
    # gateway box. In that case, load FYC_* vars from the gateway's .env.
    _load_env_file(DEFAULT_GATEWAY_ENV_FILE, prefix="FYC_")
    _load_env_file(DEFAULT_FYC_ENV_FILE, prefix="FYC_")

    payload = _read_input()

    base_url = os.environ.get("FYC_API_BASE_URL")
    if not base_url:
        host = os.environ.get("FYC_HOST", "127.0.0.1")
        port = os.environ.get("FYC_PORT", "8123")
        base_url = f"http://{host}:{port}"
    base_url = base_url.rstrip("/")
    endpoint = f"{base_url}/v1/videos/generations"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    timeout_sec = int(os.environ.get("FYC_TOOL_TIMEOUT_SEC", "3600"))

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            print(body)
            return 0
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8") if exc.fp else ""
        print(
            json.dumps(
                {
                    "error": "followyourcanvas request failed",
                    "status": exc.code,
                    "detail": err_body,
                }
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": "followyourcanvas request failed", "detail": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
