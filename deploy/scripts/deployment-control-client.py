#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


TERMINAL = {"succeeded", "failed"}


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:4000]
        raise RuntimeError(f"deployment-control HTTP {exc.code}: {detail}") from exc


def expand_component_dependencies(host: str, components: list[str]) -> list[str]:
    expanded = list(dict.fromkeys(str(item).strip() for item in components if str(item).strip()))
    # Gateway on ai2 is attached to the dedicated Cloudflare origin network by
    # docker-compose.cloudflared.yml. Recreate both together so a routine Gateway
    # deployment cannot detach the origin network and strand the public tunnel.
    if host == "ai2" and "gateway" in expanded and "cloudflared" not in expanded:
        expanded.append("cloudflared")
    return expanded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a centralized Nexus deployment")
    parser.add_argument("--host", required=True)
    parser.add_argument("--component", action="append", default=[])
    parser.add_argument("--components", default="")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--environment", default="prod", choices=("prod",))
    parser.add_argument("--reason", default="")
    parser.add_argument("--requested-by", default=os.getenv("USER", "agent"))
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument(
        "--url", default=os.getenv("DEPLOY_CONTROL_URL", "http://127.0.0.1:9220")
    )
    parser.add_argument(
        "--token-file",
        default=os.getenv(
            "DEPLOY_CONTROL_TOKEN_FILE",
            "/data/nexus-runtime/deployment-control/token",
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    components = list(args.component)
    components.extend(item.strip() for item in args.components.split(",") if item.strip())
    components = expand_component_dependencies(args.host, components)
    if not components:
        raise SystemExit("at least one --component or --components value is required")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"deployment-control token file is empty: {args.token_file}")
    payload = request_json(
        "POST",
        f"{args.url.rstrip('/')}/v1/deployments",
        token,
        {
            "host": args.host,
            "components": components,
            "branch": args.branch,
            "environment": args.environment,
            "reason": args.reason,
            "requested_by": args.requested_by,
        },
    )
    job_id = str(payload.get("id") or "")
    print(json.dumps({"id": job_id, "status": payload.get("status")}, sort_keys=True))
    if args.no_wait:
        return 0
    deadline = time.monotonic() + max(1, args.timeout_sec)
    last_status = ""
    while time.monotonic() < deadline:
        current = request_json(
            "GET", f"{args.url.rstrip('/')}/v1/deployments/{job_id}", token
        )
        status = str(current.get("status") or "")
        if status != last_status:
            print(f"deployment {job_id}: {status}", file=sys.stderr)
            last_status = status
        if status in TERMINAL:
            if status == "succeeded":
                return 0
            for line in current.get("log_tail") or []:
                print(str(line), file=sys.stderr)
            print(str(current.get("error") or "deployment failed"), file=sys.stderr)
            return 1
        time.sleep(2)
    print(f"deployment {job_id} did not finish within {args.timeout_sec}s", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
