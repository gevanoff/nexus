from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from fastapi import HTTPException, Request

from app.config import S, logger
from app.models import AgentRunRequest, AgentSpecModel, ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec
from app.openai_utils import new_id, now_unix
from app.router import decide_route
from app.router_cfg import router_cfg
from app.tools_bus import TOOL_SCHEMAS, run_tool_call
from app.upstreams import call_mlx_openai, call_ollama

Backend = Literal["ollama", "mlx"]


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _agent_runs_log_path() -> str:
    return (getattr(S, "AGENT_RUNS_LOG_PATH", "") or "/var/lib/gateway/data/agent/runs.jsonl").strip()


def _agent_runs_log_dir() -> str:
    return (getattr(S, "AGENT_RUNS_LOG_DIR", "") or "/var/lib/gateway/data/agent").strip()


def _agent_runs_log_mode() -> str:
    return getattr(S, "AGENT_RUNS_LOG_MODE", "per_run")


def _write_jsonl_line(path: str, event: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = _canonical_json(event)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _write_run_file(run_id: str, payload: Dict[str, Any]) -> None:
    base_dir = _agent_runs_log_dir()
    os.makedirs(base_dir, exist_ok=True)
    path = os.path.join(base_dir, f"{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_canonical_json(payload))
        f.write("\n")


def _persist_run(run_id: str, payload: Dict[str, Any]) -> None:
    mode = _agent_runs_log_mode()
    if mode in ("ndjson", "both"):
        _write_jsonl_line(_agent_runs_log_path(), payload)
    if mode in ("per_run", "both"):
        _write_run_file(run_id, payload)


def load_agent_specs() -> Dict[str, AgentSpecModel]:
    """Load agent specs from a fixed JSON file.

    File format:
      {
        "default": {"model": "fast", "tier": 0, "max_turns": 8, ...},
        "heavy": {"model": "coder", "tier": 2, ...}
      }

    If the file is missing/unreadable, falls back to a minimal default spec.
    """

    path = (getattr(S, "AGENT_SPECS_PATH", "") or "").strip()
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out: Dict[str, AgentSpecModel] = {}
                for k, v in obj.items():
                    if not isinstance(k, str) or not k.strip() or not isinstance(v, dict):
                        continue
                    try:
                        out[k.strip()] = AgentSpecModel(**v)
                    except Exception:
                        continue
                if out:
                    return out
        except Exception:
            pass

    return {
        "default": AgentSpecModel(model="fast", tier=0, max_turns=8, max_runtime_sec=60.0, max_total_tool_io_bytes=2_000_000)
    }


def tools_for_tier(tier: int) -> set[str]:
    """Capability tiers: agents can only be granted tiers explicitly."""

    # Tier 0: read-only FS + restricted HTTP GET.
    t0 = {"read_file", "http_fetch_local", "noop"}

    # Tier 1: write FS + structured DB ops.
    # Include media/music generation tools like HeartMula in tier 1 so agents configured
    # with tier >= 1 may be granted access when the agent spec explicitly allowlists them.
    t1 = t0 | {"write_file", "memory_v2_upsert", "memory_v2_search", "memory_v2_list", "memory_v2_delete", "heartmula_generate"}

    # Tier 2: shell execution (highly constrained, opt-in).
    t2 = t1 | {"shell"}

    if tier <= 0:
        return t0
    if tier == 1:
        return t1
    return t2


def _tool_specs_for_names(names: Sequence[str]) -> List[ToolSpec]:
    out: List[ToolSpec] = []
    for n in names:
        sch = TOOL_SCHEMAS.get(n)
        if not isinstance(sch, dict):
            continue
        params = sch.get("parameters")
        desc = sch.get("description")
        if not isinstance(params, dict):
            continue
        if not isinstance(desc, str):
            desc = ""
        out.append(
            ToolSpec(
                function=ToolFunction(
                    name=str(sch.get("name") or n),
                    description=desc,
                    parameters=params,
                )
            )
        )
    return out


@dataclass(frozen=True)
class AdmissionProfile:
    concurrency: int
    queue_max: int
    queue_timeout_sec: float


class DeterministicAdmissionControl:
    """Single-process admission control with deterministic refusal rules."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sem: dict[Backend, asyncio.Semaphore] = {}
        self._waiters: dict[Backend, int] = {"ollama": 0, "mlx": 0}

    def _profile(self, backend: Backend) -> AdmissionProfile:
        if backend == "mlx":
            conc = int(getattr(S, "AGENT_BACKEND_CONCURRENCY_MLX", 2) or 2)
        else:
            conc = int(getattr(S, "AGENT_BACKEND_CONCURRENCY_OLLAMA", 4) or 4)
        queue_max = int(getattr(S, "AGENT_QUEUE_MAX", 32) or 32)
        timeout_sec = float(getattr(S, "AGENT_QUEUE_TIMEOUT_SEC", 2.0) or 2.0)
        return AdmissionProfile(concurrency=max(1, conc), queue_max=max(0, queue_max), queue_timeout_sec=max(0.0, timeout_sec))

    async def acquire(self, *, backend: Backend, tier: int) -> "_AdmissionLease":
        shed = bool(getattr(S, "AGENT_SHED_HEAVY", True))
        if shed and tier >= 1:
            raise HTTPException(status_code=429, detail={"error": "shed_heavy", "error_type": "rate_limited", "error_message": "heavy agents refused (shed heavy mode)"})

        prof = self._profile(backend)
        async with self._lock:
            sem = self._sem.get(backend)
            if sem is None or getattr(sem, "_initial_value", None) != prof.concurrency:  # type: ignore[attr-defined]
                # Recreate if config changed.
                sem = asyncio.Semaphore(prof.concurrency)
                setattr(sem, "_initial_value", prof.concurrency)  # type: ignore[attr-defined]
                self._sem[backend] = sem

            if prof.queue_max >= 0 and self._waiters.get(backend, 0) >= prof.queue_max:
                raise HTTPException(status_code=429, detail={"error": "queue_full", "error_type": "rate_limited", "error_message": "agent queue full"})

            self._waiters[backend] = int(self._waiters.get(backend, 0)) + 1

        acquired = False
        try:
            await asyncio.wait_for(sem.acquire(), timeout=prof.queue_timeout_sec)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(status_code=429, detail={"error": "queue_timeout", "error_type": "rate_limited", "error_message": "agent queue timeout"})
        finally:
            async with self._lock:
                self._waiters[backend] = max(0, int(self._waiters.get(backend, 0)) - 1)

        if not acquired:
            raise HTTPException(status_code=429, detail={"error": "rate_limited", "error_type": "rate_limited", "error_message": "agent capacity exceeded"})

        return _AdmissionLease(sem)


class _AdmissionLease:
    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._released = False

    async def __aenter__(self) -> "_AdmissionLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._sem.release()
        except Exception:
            return


_ADMISSION = DeterministicAdmissionControl()


def load_transcript(run_id: str) -> Dict[str, Any]:
    rid = (run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail={"error": "invalid run id", "error_type": "invalid_request", "error_message": "run_id must be a non-empty string"})

    # Prefer per-run file.
    try:
        p = os.path.join(_agent_runs_log_dir(), f"{rid}.json")
        if os.path.exists(p):
            raw = Path(p).read_text(encoding="utf-8")
            return json.loads(raw)
    except Exception:
        pass

    # Fallback: scan NDJSON log for the last matching run_id.
    try:
        path = _agent_runs_log_path()
        if os.path.exists(path):
            last = None
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("run_id") == rid:
                        last = obj
            if isinstance(last, dict):
                return last
    except Exception:
        pass

    raise HTTPException(status_code=404, detail={"error": f"run not found: {rid}", "error_type": "replay_not_found", "error_message": f"run not found: {rid}"})


def _extract_assistant_message(resp: Dict[str, Any]) -> ChatMessage:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    if not isinstance(msg, dict):
        msg = {}
    role = msg.get("role") if isinstance(msg.get("role"), str) else "assistant"
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    return ChatMessage(role=role, content=content, tool_calls=tool_calls)


def _extract_tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    tc = (msg or {}).get("tool_calls")
    if isinstance(tc, list):
        return [t for t in tc if isinstance(t, dict)]
    return []


def _tool_message_for_result(*, tool_call_id: str, result: Dict[str, Any]) -> ChatMessage:
    return ChatMessage(role="tool", tool_call_id=tool_call_id, content=json.dumps(result, separators=(",", ":"), ensure_ascii=False))


async def run_agent_v1(*, req: Request, run_req: AgentRunRequest) -> Tuple[Dict[str, Any], Backend, str]:
    specs = load_agent_specs()
    spec = specs.get(run_req.agent) or specs.get("default")
    if spec is None:
        raise HTTPException(status_code=400, detail="unknown agent")

    tier = int(spec.tier or 0)

    # Build initial messages.
    if run_req.messages is not None:
        messages = list(run_req.messages)
    else:
        user_input = run_req.input
        if not isinstance(user_input, str) or not user_input.strip():
            raise HTTPException(status_code=400, detail="input must be a non-empty string (or provide messages)")
        messages = [ChatMessage(role="user", content=user_input)]

    # Apply agent-spec tool allowlist, intersected with tier capability set.
    allowed = tools_for_tier(tier)
    if spec.tools_allowlist:
        allow2 = {t.strip() for t in spec.tools_allowlist if isinstance(t, str) and t.strip()}
        allowed = allowed.intersection(allow2)

    tools = _tool_specs_for_names(sorted(allowed)) if allowed else None

    # Route once, and stick to a fixed backend/model for determinism.
    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=router_cfg(),
        request_model=spec.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in messages],
        has_tools=bool(tools),
        enable_policy=getattr(S, "ROUTER_ENABLE_POLICY", True),
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )
    backend: Backend = route.backend
    upstream_model = route.model

    # Admission control by backend.
    async with (await _ADMISSION.acquire(backend=backend, tier=tier)):
        t0 = time.monotonic()
        run_id = new_id("run")
        request_hash = _sha256_hex(
            _canonical_json(
                {
                    "agent": run_req.agent,
                    "spec": spec.model_dump(),
                    "messages": [m.model_dump(exclude_none=True) for m in messages],
                    "backend": backend,
                    "upstream_model": upstream_model,
                }
            )
        )

        events: List[Dict[str, Any]] = []
        total_tool_io = 0

        def _emit(ev: Dict[str, Any]) -> None:
            events.append(ev)

        _emit(
            {
                "ts": now_unix(),
                "type": "run_started",
                "run_id": run_id,
                "request_hash": request_hash,
                "agent": run_req.agent,
                "tier": tier,
                "backend": backend,
                "upstream_model": upstream_model,
                "max_turns": int(spec.max_turns),
            }
        )

        output_text = ""
        ok = False
        error: Optional[str] = None

        try:
            max_turns = int(spec.max_turns or 0)
            if max_turns <= 0:
                raise HTTPException(status_code=400, detail="agent max_turns must be > 0")

            max_runtime_sec = float(spec.max_runtime_sec) if spec.max_runtime_sec is not None else None
            max_tool_io = int(spec.max_total_tool_io_bytes) if spec.max_total_tool_io_bytes is not None else None

            # Deterministic system prompt for the planning phase.
            system_plan = ChatMessage(
                role="system",
                content=(
                    "You are AgentRuntimeV1. Follow a strict loop: PLAN -> (optional TOOL) -> OBSERVE -> NEXT -> TERMINATE. "
                    "Do not exceed the user's budgets. Be concise."
                ),
            )

            for turn in range(max_turns):
                if max_runtime_sec is not None and (time.monotonic() - t0) > max_runtime_sec:
                    raise HTTPException(status_code=408, detail="agent runtime budget exceeded")

                # PLAN step: no tools.
                plan_req = ChatCompletionRequest(
                    model=upstream_model if backend == "mlx" else spec.model,
                    messages=[system_plan, *messages],
                    stream=False,
                )

                plan_resp = await (call_mlx_openai(plan_req) if backend == "mlx" else call_ollama(plan_req, upstream_model))
                plan_msg = _extract_assistant_message(plan_resp)
                _emit(
                    {
                        "ts": now_unix(),
                        "type": "plan",
                        "turn": turn,
                        "message": plan_msg.model_dump(exclude_none=True),
                    }
                )
                messages.append(plan_msg)

                # ACTION step: tools enabled.
                action_req = ChatCompletionRequest(
                    model=upstream_model if backend == "mlx" else spec.model,
                    messages=messages,
                    tools=tools,
                    stream=False,
                )

                action_resp = await (
                    call_mlx_openai(action_req) if backend == "mlx" else call_ollama(action_req, upstream_model)
                )

                action_msg = _extract_assistant_message(action_resp)
                _emit(
                    {
                        "ts": now_unix(),
                        "type": "assistant",
                        "turn": turn,
                        "message": action_msg.model_dump(exclude_none=True),
                    }
                )

                tool_calls = _extract_tool_calls(action_resp)
                messages.append(action_msg)

                if not tool_calls:
                    content = action_msg.content
                    output_text = content if isinstance(content, str) else ""
                    ok = True
                    break

                for tc in tool_calls:
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name")
                    arguments = fn.get("arguments", "")
                    tool_call_id = tc.get("id") if isinstance(tc.get("id"), str) else ""
                    if not tool_call_id:
                        tool_call_id = new_id("toolcall")

                    if not isinstance(name, str) or not name.strip():
                        raise HTTPException(status_code=502, detail="invalid tool call from model")

                    if max_runtime_sec is not None and (time.monotonic() - t0) > max_runtime_sec:
                        raise HTTPException(status_code=408, detail="agent runtime budget exceeded")

                    tool_res = run_tool_call(name.strip(), arguments if isinstance(arguments, str) else "", allowed_tools=set(allowed))

                    try:
                        io_bytes = tool_res.get("tool_io_bytes")
                        if isinstance(io_bytes, (int, float)):
                            total_tool_io += int(io_bytes)
                    except Exception:
                        pass

                    _emit(
                        {
                            "ts": now_unix(),
                            "type": "tool",
                            "turn": turn,
                            "tool_call_id": tool_call_id,
                            "name": name.strip(),
                            "result": tool_res,
                        }
                    )

                    if max_tool_io is not None and total_tool_io > max_tool_io:
                        raise HTTPException(status_code=413, detail="tool IO budget exceeded")

                    messages.append(_tool_message_for_result(tool_call_id=tool_call_id, result=tool_res))

            if not ok and error is None and not output_text:
                # Turn limit exceeded.
                raise HTTPException(status_code=408, detail="agent turn limit exceeded")

        except HTTPException as e:
            error = e.detail if isinstance(e.detail, str) else _canonical_json(e.detail)
            ok = False
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            ok = False

        _emit(
            {
                "ts": now_unix(),
                "type": "run_completed" if ok else "run_failed",
                "run_id": run_id,
                "ok": ok,
                "output_text": output_text,
                "error": error,
                "total_tool_io_bytes": total_tool_io,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            }
        )

        payload = {
            "run_id": run_id,
            "request_hash": request_hash,
            "agent": run_req.agent,
            "tier": tier,
            "backend": backend,
            "upstream_model": upstream_model,
            "ok": ok,
            "output_text": output_text,
            "error": error,
            "events": events,
        }

        try:
            _persist_run(run_id, payload)
        except Exception:
            logger.exception("agent run persistence failed")

        return payload, backend, upstream_model
