#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import ssl
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_TLS_CONTEXT: ssl.SSLContext | None = None


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


def _maybe_reexec_into_gateway_venv() -> None:
    """Re-exec into the gateway venv python when available.

    Mirrors the deployment layout used by ai-infra on macOS/Linux.
    """

    if os.getenv("GATEWAY_SKIP_REEXEC") == "1":
        return

    candidates: list[str] = []
    override = (os.getenv("GATEWAY_VENV_PY") or "").strip()
    if override:
        candidates.append(override)

    candidates.extend(
        [
            "/var/lib/gateway/env/bin/python",
            "/var/lib/gateway/venv/bin/python",
        ]
    )

    try:
        for venv_py in candidates:
            if os.path.exists(venv_py) and os.path.realpath(sys.executable) != os.path.realpath(venv_py):
                env = dict(os.environ)
                env["GATEWAY_SKIP_REEXEC"] = "1"
                os.execve(venv_py, [venv_py, *sys.argv], env)
    except Exception:
        return


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


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


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    metrics: dict[str, Any] | None = None


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 20.0,
    max_body_bytes: int = 512_000,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method=method.upper())

    try:
        with urlopen(request, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read(max_body_bytes + 1)
            return status, resp_headers, data[:max_body_bytes]
    except HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        try:
            data = e.read(max_body_bytes + 1)
        except Exception:
            data = b""
        return status, {k.lower(): v for k, v in getattr(e, "headers", {}).items()}, data[:max_body_bytes]
    except URLError as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def _json_from_bytes(b: bytes) -> object:
    if not b:
        raise ValueError("empty body")
    return json.loads(b.decode("utf-8"))


def _http_stream_metrics(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 30.0,
    max_bytes: int = 512_000,
) -> dict[str, Any]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method="POST")

    t0 = time.monotonic()
    ttft_ms: float | None = None
    done = False
    total_bytes = 0

    try:
        with urlopen(request, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            # Read line-by-line to measure first SSE event.
            while True:
                line = resp.readline()
                if not line:
                    break
                if isinstance(line, str):
                    line_b = line.encode("utf-8")
                else:
                    line_b = bytes(line)

                total_bytes += len(line_b)
                if total_bytes > max_bytes:
                    break

                if ttft_ms is None and line_b.startswith(b"data:"):
                    ttft_ms = round((time.monotonic() - t0) * 1000.0, 1)

                if line_b.strip() == b"data: [DONE]":
                    done = True
                    break

            total_ms = round((time.monotonic() - t0) * 1000.0, 1)
            return {"ok": status == 200 and done, "status": status, "ttft_ms": ttft_ms, "total_ms": total_ms, "done": done, "bytes": total_bytes}
    except HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        total_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return {"ok": False, "status": status, "ttft_ms": ttft_ms, "total_ms": total_ms, "done": False, "bytes": total_bytes}


def _mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_json(path: str, obj: dict) -> None:
    _mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True))
        f.write("\n")


def _append_jsonl(path: str, obj: dict) -> None:
    _mkdir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), sort_keys=True))
        f.write("\n")


def _load_golden(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        raw = open(path, "r", encoding="utf-8").read()
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _run_checks(*, base_url: str, obs_url: str, token: str, require_backend: bool, golden_path: str, ttft_budget_ms: float, total_budget_ms: float) -> tuple[list[CheckResult], dict[str, Any]]:
    checks: list[CheckResult] = []
    meta: dict[str, Any] = {
        "base_url": base_url,
        "require_backend": require_backend,
        "ttft_budget_ms": ttft_budget_ms,
        "total_budget_ms": total_budget_ms,
    }

    bearer = {"authorization": f"Bearer {token}"}
    v1 = base_url.rstrip("/") + "/v1"

    # Health / upstream status
    backends_ok = False
    try:
        status, _h, body = _http_request("GET", obs_url.rstrip("/") + "/health/upstreams", headers=bearer, timeout_sec=5.0)
        if status == 200:
            payload = _json_from_bytes(body)
            ok = isinstance(payload, dict) and any(bool(v.get("ok")) for v in payload.values() if isinstance(v, dict))
            backends_ok = bool(ok)
            checks.append(CheckResult("health_upstreams", True, detail="ok" if ok else "no healthy upstreams", metrics={"backends_ok": backends_ok}))
        else:
            checks.append(CheckResult("health_upstreams", False, detail=f"status={status}"))
    except Exception as e:
        checks.append(CheckResult("health_upstreams", False, detail=f"{type(e).__name__}: {e}"))

    if require_backend and not backends_ok:
        checks.append(CheckResult("backend_required", False, detail="no healthy upstreams reported"))
        return checks, meta

    # Routing correctness (basic): models + headers present on chat.
    try:
        status, _h, body = _http_request("GET", v1 + "/models", headers=bearer, timeout_sec=10.0)
        if status == 200:
            checks.append(CheckResult("models_list", True))
        else:
            checks.append(CheckResult("models_list", False, detail=f"status={status} body={body[:200]!r}"))
    except Exception as e:
        checks.append(CheckResult("models_list", False, detail=f"{type(e).__name__}: {e}"))

    # Tool correctness: schema list + noop execution.
    try:
        status, _h, body = _http_request("GET", v1 + "/tools", headers=bearer, timeout_sec=10.0)
        if status == 200:
            payload = _json_from_bytes(body)
            ok = isinstance(payload, list)
            checks.append(CheckResult("tools_list", ok, detail="" if ok else "unexpected body"))
        else:
            checks.append(CheckResult("tools_list", False, detail=f"status={status}"))
    except Exception as e:
        checks.append(CheckResult("tools_list", False, detail=f"{type(e).__name__}: {e}"))

    try:
        status, _h, body = _http_request("POST", v1 + "/tools/noop", headers=bearer, json_body={"arguments": {"text": "eval"}}, timeout_sec=20.0)
        if status == 200:
            payload = _json_from_bytes(body)
            ok = isinstance(payload, dict) and payload.get("ok") is True and isinstance(payload.get("replay_id"), str)
            checks.append(CheckResult("tool_noop", ok, detail="" if ok else "unexpected body"))
        else:
            checks.append(CheckResult("tool_noop", False, detail=f"status={status} body={body[:200]!r}"))
    except Exception as e:
        checks.append(CheckResult("tool_noop", False, detail=f"{type(e).__name__}: {e}"))

    # Latency budgets (TTFT + total) via streaming chat.
    if backends_ok:
        try:
            stream_headers = dict(bearer)
            stream_headers["accept"] = "text/event-stream"
            m = _http_stream_metrics(
                v1 + "/chat/completions",
                headers=stream_headers,
                json_body={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Say hi."}]},
                timeout_sec=max(10.0, total_budget_ms / 1000.0 + 10.0),
            )
            ok = bool(m.get("ok"))
            ttft = m.get("ttft_ms")
            total = m.get("total_ms")
            if isinstance(ttft, (int, float)) and float(ttft) > ttft_budget_ms:
                ok = False
            if isinstance(total, (int, float)) and float(total) > total_budget_ms:
                ok = False
            checks.append(CheckResult("latency_chat_stream", ok, metrics=m))
        except Exception as e:
            checks.append(CheckResult("latency_chat_stream", False, detail=f"{type(e).__name__}: {e}"))

    # Retrieval relevance (golden queries) - optional.
    golden = _load_golden(golden_path) or {}
    retrieval = golden.get("retrieval") if isinstance(golden, dict) else None
    if isinstance(retrieval, list) and retrieval:
        passed = 0
        total = 0
        details: list[dict[str, Any]] = []
        for item in retrieval:
            if not isinstance(item, dict):
                continue
            q = item.get("query")
            expect_ids = item.get("expect_ids")
            min_hits = item.get("min_hits", 1)
            if not isinstance(q, str) or not q.strip():
                continue
            if not isinstance(min_hits, int):
                min_hits = 1
            total += 1
            try:
                status, _h, body = _http_request(
                    "POST",
                    v1 + "/memory/search",
                    headers=bearer,
                    json_body={"query": q, "top_k": int(item.get("top_k") or 6), "min_sim": float(item.get("min_sim") or 0.25)},
                    timeout_sec=20.0,
                )
                if status != 200:
                    details.append({"query": q, "ok": False, "status": status})
                    continue
                payload = _json_from_bytes(body)
                results = payload.get("results") if isinstance(payload, dict) else None
                ids = [r.get("id") for r in results] if isinstance(results, list) else []
                hit = 0
                if isinstance(expect_ids, list) and expect_ids:
                    want = {str(x) for x in expect_ids if isinstance(x, str)}
                    hit = sum(1 for i in ids if isinstance(i, str) and i in want)
                else:
                    # If no expected ids, treat as informational.
                    hit = len(ids)
                ok = hit >= int(min_hits)
                if ok:
                    passed += 1
                details.append({"query": q, "ok": ok, "hits": hit, "ids": ids[:10]})
            except Exception as e:
                details.append({"query": q, "ok": False, "error": f"{type(e).__name__}: {e}"})

        checks.append(CheckResult("retrieval_golden", passed == total, detail=f"{passed}/{total}", metrics={"passed": passed, "total": total, "cases": details}))
    else:
        checks.append(CheckResult("retrieval_golden", True, detail="skipped (no golden file)", metrics={"skipped": True}))

    return checks, meta


def main() -> int:
    _maybe_reexec_into_gateway_venv()

    ap = argparse.ArgumentParser(description="Local eval harness for the gateway (quality/safety/regressions).")
    ap.add_argument("--base-url", default=os.getenv("GATEWAY_BASE_URL", "https://127.0.0.1:8800"), help="Gateway base URL")
    ap.add_argument("--obs-url", default=os.getenv("GATEWAY_OBS_URL", ""), help="Observability base URL (defaults to derive from base URL)")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed local certs).",
    )
    ap.add_argument(
        "--token",
        default="",
        help="Bearer token (default: $GATEWAY_BEARER_TOKEN or first of $GATEWAY_BEARER_TOKENS)",
    )
    ap.add_argument("--require-backend", action="store_true", help="Fail if no healthy upstreams")
    ap.add_argument("--golden", default=os.getenv("GATEWAY_EVAL_GOLDEN", ""), help="Path to golden eval JSON (optional)")
    ap.add_argument("--out-dir", default=os.getenv("GATEWAY_EVALS_DIR", "/var/lib/gateway/data/evals"), help="Directory for reports/history")
    ap.add_argument("--ttft-budget-ms", type=float, default=float(os.getenv("GATEWAY_EVAL_TTFT_BUDGET_MS", "1500")), help="TTFT budget for streaming checks")
    ap.add_argument("--total-budget-ms", type=float, default=float(os.getenv("GATEWAY_EVAL_TOTAL_BUDGET_MS", "15000")), help="Total completion time budget")
    ap.add_argument("--start-server", action="store_true", help="Start uvicorn locally for the eval run (dev convenience)")
    args = ap.parse_args()

    args.token = (args.token or "").strip() or _env_gateway_token()

    insecure = args.insecure or (os.getenv("GATEWAY_TLS_INSECURE") or "").strip().lower() in {"1", "true", "yes", "on"}
    global _TLS_CONTEXT
    if insecure:
        _TLS_CONTEXT = ssl._create_unverified_context()
    else:
        _TLS_CONTEXT = ssl.create_default_context()

    if not args.token:
        print("Missing token. Set --token or GATEWAY_BEARER_TOKEN.")
        return 2

    out_dir = str(args.out_dir)
    history_path = os.path.join(out_dir, "history.jsonl")

    server_proc: subprocess.Popen | None = None
    base_url = str(args.base_url)
    obs_url = str(args.obs_url or "").strip()

    if args.start_server:
        # Start a local uvicorn for evals (best-effort). This is for dev usage; production uses launchd/systemd.
        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        obs_url = base_url
        cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
        server_proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.dirname(__file__)))
        # Give it a moment.
        time.sleep(0.8)

    started = int(time.time())
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    try:
        checks, meta = _run_checks(
            base_url=base_url,
            obs_url=(obs_url or _derive_obs_url(base_url)),
            token=str(args.token),
            require_backend=bool(args.require_backend),
            golden_path=str(args.golden or ""),
            ttft_budget_ms=float(args.ttft_budget_ms),
            total_budget_ms=float(args.total_budget_ms),
        )

        ok = all(c.ok for c in checks)
        report = {
            "ts": started,
            "ts_iso": started_iso,
            "ok": ok,
            "meta": meta,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "metrics": c.metrics,
                }
                for c in checks
            ],
        }

        report_name = time.strftime("report-%Y%m%d-%H%M%S.json", time.gmtime(started))
        report_path = os.path.join(out_dir, report_name)
        latest_path = os.path.join(out_dir, "latest.json")

        _write_json(report_path, report)
        _write_json(latest_path, report)

        # Trend history: keep a compact summary for easy plotting.
        summary = {
            "ts": started,
            "ts_iso": started_iso,
            "ok": ok,
            "checks": {c.name: {"ok": c.ok, "detail": c.detail, "metrics": c.metrics} for c in checks},
        }
        _append_jsonl(history_path, summary)

        print(f"Wrote report: {report_path}")
        print(f"Updated latest: {latest_path}")
        print(f"Appended history: {history_path}")

        return 0 if ok else 1
    finally:
        if server_proc is not None:
            try:
                server_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
