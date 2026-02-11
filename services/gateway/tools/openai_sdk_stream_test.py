#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, List, Optional


def _maybe_reexec_into_gateway_venv() -> None:
    """If the gateway venv exists, ensure we run under it.

    On the macOS host, this script is typically executed as `tools/openai_sdk_stream_test.py`,
    which uses the system python from the shebang. The gateway runtime, however, uses
    /var/lib/gateway/env/bin/python. Re-exec into that interpreter if present so
    optional deps (like `openai`) are resolved from the correct environment.
    """

    if os.getenv("GATEWAY_SKIP_REEXEC") == "1":
        return

    candidates = []
    override = (os.getenv("GATEWAY_VENV_PY") or "").strip()
    if override:
        candidates.append(override)
    # Default paths used by ai-infra scripts
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
        # Fall back to current interpreter; the error message below will
        # explain how to install deps / run with the venv python.
        return


def _env_first(*keys: str) -> Optional[str]:
    for k in keys:
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return None


def _env_flag(name: str) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _raw_sse_debug(base_url: str, api_key: str, model: str, prompt: str, *, insecure: bool) -> None:
    try:
        import httpx

        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"authorization": f"Bearer {api_key}", "accept": "text/event-stream", "content-type": "application/json"}
        payload = {"model": model, "stream": True, "messages": [{"role": "user", "content": prompt}]}

        print("---- raw http debug ----", file=sys.stderr)
        print(f"POST {url}", file=sys.stderr)
        with httpx.Client(timeout=30.0, http2=False, verify=not insecure) as hc:
            with hc.stream("POST", url, headers=headers, json=payload) as r:
                print(f"status={r.status_code}", file=sys.stderr)
                ct = r.headers.get("content-type", "")
                print(f"content-type={ct}", file=sys.stderr)
                for hk in ["x-backend-used", "x-model-used", "x-router-reason"]:
                    if hk in r.headers:
                        print(f"{hk}={r.headers.get(hk)}", file=sys.stderr)

                buf = bytearray()
                tail = bytearray()
                saw_done = False
                saw_finish_reason = False
                saw_content_field = False
                data_frames = 0

                # Read a bounded amount; enough to see ordering and whether any content was streamed.
                for chunk in r.iter_bytes():
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    tail.extend(chunk)
                    if len(tail) > 4096:
                        tail = tail[-4096:]

                    # Heuristic counters (good enough for diagnostics).
                    data_frames += chunk.count(b"\ndata:") + (1 if chunk.startswith(b"data:") else 0)
                    if b"data: [DONE]" in buf:
                        saw_done = True
                    if b"\"finish_reason\":" in buf:
                        saw_finish_reason = True
                    if b"\"content\":" in buf:
                        saw_content_field = True

                    if saw_done:
                        break
                    if len(buf) >= 65_536:
                        break

                preview = bytes(buf)
                print(f"data_frames_estimate={data_frames}", file=sys.stderr)
                print(f"saw_done={saw_done}", file=sys.stderr)
                print(f"saw_finish_reason_field={saw_finish_reason}", file=sys.stderr)
                print(f"saw_content_field={saw_content_field}", file=sys.stderr)
                if preview:
                    print("first_bytes=", file=sys.stderr)
                    print(preview[:4096].decode("utf-8", errors="replace"), file=sys.stderr)
                    print("last_bytes=", file=sys.stderr)
                    print(bytes(tail).decode("utf-8", errors="replace"), file=sys.stderr)
                else:
                    print("first_bytes=(none)", file=sys.stderr)
    except Exception as e:
        print(f"raw debug failed: {type(e).__name__}: {e}", file=sys.stderr)


def main(argv: List[str]) -> int:
    _maybe_reexec_into_gateway_venv()

    p = argparse.ArgumentParser(description="Validate gateway SSE streaming via the OpenAI Python SDK.")
    p.add_argument("--base-url", default=_env_first("GATEWAY_OPENAI_BASE_URL", "OPENAI_BASE_URL") or "https://127.0.0.1:8800/v1")
    p.add_argument("--api-key", default=_env_first("GATEWAY_BEARER_TOKEN", "OPENAI_API_KEY") or "")
    p.add_argument("--model", default=os.getenv("GATEWAY_MODEL", "fast"))
    p.add_argument("--prompt", default="Count from 1 to 5, slowly.")
    p.add_argument("--max-chunks", type=int, default=10_000)
    p.add_argument("--debug-http", action="store_true", help="If set, also print raw HTTP/SSE details.")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed local certs).",
    )
    ns = p.parse_args(argv)

    if not ns.api_key:
        print("ERROR: missing api key. Set GATEWAY_BEARER_TOKEN (recommended) or pass --api-key.", file=sys.stderr)
        return 2

    try:
        # openai>=1.x
        from openai import OpenAI  # type: ignore
    except Exception as e:
        print("ERROR: openai Python package not installed in this environment.", file=sys.stderr)
        if os.path.exists("/var/lib/gateway/env/bin/python"):
            print(
                "Install with: sudo -u gateway /var/lib/gateway/env/bin/python -m pip install -r /var/lib/gateway/app/tools/requirements.txt",
                file=sys.stderr,
            )
            print(
                "If /var/lib/gateway/env does not exist yet, run: sudo ai-infra/services/gateway/scripts/install.sh",
                file=sys.stderr,
            )
        else:
            print(
                "Install with (preferred): sudo -u gateway /var/lib/gateway/env/bin/python -m pip install -r /var/lib/gateway/app/tools/requirements.txt",
                file=sys.stderr,
            )
            print("Or (current python): python3 -m pip install openai", file=sys.stderr)
        print(f"Import error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    insecure = ns.insecure or _env_flag("GATEWAY_TLS_INSECURE")
    try:
        import httpx

        http_client = httpx.Client(verify=not insecure)
    except Exception:
        http_client = None

    if http_client is not None:
        client = OpenAI(base_url=ns.base_url, api_key=ns.api_key, http_client=http_client)
    else:
        client = OpenAI(base_url=ns.base_url, api_key=ns.api_key)

    print(f"base_url={ns.base_url}")
    print(f"model={ns.model}")

    chunks = 0
    text_out = []
    finish_reason: Optional[str] = None

    try:
        # Be explicit about accepting SSE.
        try:
            if http_client is not None:
                client = OpenAI(
                    base_url=ns.base_url,
                    api_key=ns.api_key,
                    default_headers={"accept": "text/event-stream"},
                    http_client=http_client,
                )
            else:
                client = OpenAI(base_url=ns.base_url, api_key=ns.api_key, default_headers={"accept": "text/event-stream"})
        except Exception:
            if http_client is not None:
                client = OpenAI(base_url=ns.base_url, api_key=ns.api_key, http_client=http_client)
            else:
                client = OpenAI(base_url=ns.base_url, api_key=ns.api_key)

        stream = client.chat.completions.create(
            model=ns.model,
            stream=True,
            messages=[{"role": "user", "content": ns.prompt}],
        )

        for event in stream:
            chunks += 1
            if chunks > ns.max_chunks:
                print("ERROR: exceeded --max-chunks; stream may be hanging.", file=sys.stderr)
                if http_client is not None:
                    http_client.close()
                return 4

            choice = (getattr(event, "choices", None) or [None])[0]
            if not choice:
                continue

            delta: Any = getattr(choice, "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    text_out.append(content)
                    print(content, end="", flush=True)

            fr = getattr(choice, "finish_reason", None)
            if isinstance(fr, str) and fr:
                finish_reason = fr

        print("\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
        if http_client is not None:
            http_client.close()
        return 130
    except Exception as e:
        print(f"ERROR: SDK streaming call failed: {type(e).__name__}: {e}", file=sys.stderr)
        if http_client is not None:
            http_client.close()
        return 5

    if chunks == 0:
        print("ERROR: received 0 streamed events.", file=sys.stderr)

        # This usually means either:
        # - the server only sent the terminal [DONE] marker (no JSON chunks)
        # - the server did not stream at all / returned a non-SSE response
        # Dump some raw HTTP details to make this obvious.
        _raw_sse_debug(ns.base_url, ns.api_key, ns.model, ns.prompt, insecure=insecure)

        print(
            "Hint: try an explicit Ollama model like --model ollama:qwen3:30b (or an alias mapped to Ollama, e.g. --model coder).",
            file=sys.stderr,
        )
        if http_client is not None:
            http_client.close()
        return 6

    if not finish_reason:
        print("ERROR: stream ended without a finish_reason.", file=sys.stderr)
        if ns.debug_http:
            _raw_sse_debug(ns.base_url, ns.api_key, ns.model, ns.prompt, insecure=insecure)
        if http_client is not None:
            http_client.close()
        return 7

    if ns.debug_http:
        # Run a second request using raw httpx streaming to show exactly what bytes
        # the gateway emitted (useful when the SDK reports OK but there is no content).
        _raw_sse_debug(ns.base_url, ns.api_key, ns.model, ns.prompt, insecure=insecure)

    print(f"OK: streamed {chunks} events; finish_reason={finish_reason}")
    if http_client is not None:
        http_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
