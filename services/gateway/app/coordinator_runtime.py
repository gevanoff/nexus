from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException, Request

from app.agent_runtime_v1 import _canonical_json, _persist_run, _sha256_hex
from app.backends import check_capability, get_admission_controller, get_registry
from app.config import S
from app.health_checker import check_backend_ready
from app.memory_routes import inject_memory
from app.model_aliases import get_aliases
from app.models import ChatCompletionRequest, ChatMessage, CoordinatorRunRequest
from app.openai_utils import new_id, now_unix
from app.router import _is_probably_coding_request, decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_backend_chat


def _multimodal_messages(messages: Iterable[ChatMessage]) -> bool:
    for m in messages:
        content = m.content
        if isinstance(content, (list, dict)):
            return True
    return False


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip().lower()
            if kind == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif kind == "image_url":
                parts.append("[image omitted]")
            elif kind:
                parts.append(f"[{kind} omitted]")
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(content)


def _text_only_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    return [
        ChatMessage(
            role=m.role,
            content=_content_to_text(m.content),
            name=m.name,
            tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id,
        )
        for m in messages
    ]


def _messages_to_text(messages: Sequence[ChatMessage]) -> str:
    chunks: list[str] = []
    for m in messages:
        role = (m.role or "").strip() or "message"
        text = _content_to_text(m.content).strip()
        if not text:
            continue
        chunks.append(f"{role}: {text}")
    return "\n\n".join(chunks)


def _is_vision_alias(alias_name: str) -> bool:
    key = (alias_name or "").strip().lower()
    return key in {"vision", "multimodal"}


def _dedupe_ordered(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = (item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _default_participants(messages: Sequence[ChatMessage]) -> list[str]:
    aliases = get_aliases()
    defaults = [
        part.strip()
        for part in str(getattr(S, "COORDINATOR_DEFAULT_PARTICIPANTS", "") or "").split(",")
        if part.strip()
    ]

    selected: list[str] = []
    has_media = _multimodal_messages(messages)
    if has_media and bool(getattr(S, "COORDINATOR_INCLUDE_VISION_ON_MEDIA", True)):
        for candidate in ("vision", "multimodal"):
            if candidate in aliases:
                selected.append(candidate)
                break

    selected.extend(defaults)

    raw_messages = [m.model_dump(exclude_none=True) for m in messages]
    if bool(getattr(S, "COORDINATOR_INCLUDE_CODER_ON_CODE", True)) and _is_probably_coding_request(raw_messages):
        for candidate in ("coder",):
            if candidate in aliases:
                selected.append(candidate)

    if not selected:
        selected.append("default")
    return _dedupe_ordered(selected)


def _participant_instruction(alias_name: str, extra_prompt: str | None) -> str:
    alias_key = (alias_name or "").strip().lower()
    focus = "Focus on the core answer, constraints, risks, and next steps."
    if alias_key.startswith("reasoner"):
        focus = "Focus on independent reasoning, assumptions, edge cases, and conflict detection."
    elif alias_key == "coder":
        focus = "Focus on implementation details, failure modes, debugging clues, and concrete technical recommendations."
    elif alias_key.startswith("coder"):
        focus = "Act as an independent technical check on the primary implementation. Focus on disagreements, failure modes, debugging clues, and concrete corrective recommendations."
    elif _is_vision_alias(alias_key):
        focus = "Focus on extracting and reasoning over the visual evidence in the provided media."

    prompt = (
        "You are one specialist in a multi-model coordinator. Analyze the request independently. "
        "Return a concise but substantive answer. Explicitly call out uncertainty or disagreement when present. "
        f"{focus}"
    )
    if isinstance(extra_prompt, str) and extra_prompt.strip():
        prompt = prompt + " " + extra_prompt.strip()
    return prompt


def _synthesis_instruction(extra_prompt: str | None) -> str:
    prompt = (
        "You are the synthesis model in a multi-model coordinator. Integrate the specialist responses into one final answer. "
        "Resolve disagreements when possible, flag unresolved conflicts explicitly, and avoid repeating low-signal overlap."
    )
    if isinstance(extra_prompt, str) and extra_prompt.strip():
        prompt = prompt + " " + extra_prompt.strip()
    return prompt


async def _call_coordinator_model(
    *,
    request_model: str,
    messages: list[ChatMessage],
) -> tuple[Dict[str, Any], str, str]:
    route = decide_route(
        cfg=router_cfg(),
        request_model=request_model,
        headers={},
        messages=[m.model_dump(exclude_none=True) for m in messages],
        has_tools=False,
        enable_policy=False,
    )
    backend = route.backend
    upstream_model = route.model
    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)

    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")
    try:
        resp = await call_backend_chat(
            ChatCompletionRequest(model=request_model, messages=messages, stream=False),
            backend,
            upstream_model,
        )
        return resp, backend_class, upstream_model
    finally:
        admission.release(backend_class, "chat")


def _assistant_text(resp: Dict[str, Any]) -> str:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


async def _run_participant(
    *,
    alias_name: str,
    base_messages: list[ChatMessage],
    participant_prompt: str | None,
) -> Dict[str, Any]:
    started = time.monotonic()
    needs_text_only = not _is_vision_alias(alias_name)
    participant_messages = _text_only_messages(base_messages) if needs_text_only else list(base_messages)
    participant_messages = [
        ChatMessage(role="system", content=_participant_instruction(alias_name, participant_prompt)),
        *participant_messages,
    ]

    timeout = float(getattr(S, "COORDINATOR_PARALLEL_TIMEOUT_SEC", 300.0) or 300.0)
    try:
        resp, backend, upstream_model = await asyncio.wait_for(
            _call_coordinator_model(request_model=alias_name, messages=participant_messages),
            timeout=timeout,
        )
        return {
            "participant": alias_name,
            "ok": True,
            "backend": backend,
            "upstream_model": upstream_model,
            "output_text": _assistant_text(resp),
            "response": resp,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        if not isinstance(detail, str):
            detail = _canonical_json(detail)
        return {
            "participant": alias_name,
            "ok": False,
            "error": detail,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }


async def run_coordinator_v1(*, req: Request, run_req: CoordinatorRunRequest) -> Tuple[Dict[str, Any], str, str]:
    if run_req.messages is not None:
        messages = list(run_req.messages)
    else:
        user_input = run_req.input
        if not isinstance(user_input, str) or not user_input.strip():
            raise HTTPException(status_code=400, detail="input must be a non-empty string (or provide messages)")
        messages = [ChatMessage(role="user", content=user_input)]

    messages = await inject_memory(messages, req=req)

    participants = _dedupe_ordered(run_req.participants or _default_participants(messages))
    max_participants = int(getattr(S, "COORDINATOR_MAX_PARTICIPANTS", 6) or 6)
    if len(participants) > max_participants:
        raise HTTPException(status_code=400, detail=f"too many participants (max {max_participants})")
    if not participants:
        raise HTTPException(status_code=400, detail="participants must not be empty")

    synthesizer = (run_req.synthesizer or getattr(S, "COORDINATOR_DEFAULT_SYNTHESIZER", "default") or "default").strip()
    if not synthesizer:
        synthesizer = "default"

    run_id = new_id("coord")
    request_hash = _sha256_hex(
        _canonical_json(
            {
                "messages": [m.model_dump(exclude_none=True) for m in messages],
                "participants": participants,
                "synthesizer": synthesizer,
                "participant_prompt": run_req.participant_prompt,
                "synthesis_prompt": run_req.synthesis_prompt,
            }
        )
    )

    events: List[Dict[str, Any]] = []

    def emit(event: Dict[str, Any]) -> None:
        events.append(event)

    emit(
        {
            "ts": now_unix(),
            "type": "run_started",
            "run_id": run_id,
            "request_hash": request_hash,
            "participants": participants,
            "synthesizer": synthesizer,
        }
    )

    started = time.monotonic()
    participant_results = await asyncio.gather(
        *[
            _run_participant(alias_name=alias, base_messages=messages, participant_prompt=run_req.participant_prompt)
            for alias in participants
        ]
    )

    for result in participant_results:
        emit(
            {
                "ts": now_unix(),
                "type": "participant_completed" if result.get("ok") else "participant_failed",
                **result,
            }
        )

    successful = [r for r in participant_results if r.get("ok")]
    synthesis_backend = ""
    synthesis_model = ""
    synthesis: Dict[str, Any] = {}
    output_text = ""
    ok = False
    error: Optional[str] = None

    if not successful:
        error = "all participants failed"
        synthesis = {"ok": False, "error": error}
    else:
        specialist_blocks: list[str] = []
        for item in participant_results:
            tag = str(item.get("participant") or "participant")
            if item.get("ok"):
                specialist_blocks.append(
                    f"[{tag}] backend={item.get('backend')} model={item.get('upstream_model')}\n{item.get('output_text') or ''}".strip()
                )
            else:
                specialist_blocks.append(f"[{tag}] ERROR: {item.get('error') or 'unknown error'}")

        specialist_text = "\n\n".join(specialist_blocks)
        request_text = _messages_to_text(_text_only_messages(messages))

        synth_messages = [
            ChatMessage(role="system", content=_synthesis_instruction(run_req.synthesis_prompt)),
            ChatMessage(
                role="user",
                content=(
                    "Original request:\n"
                    f"{request_text}\n\n"
                    "Specialist outputs:\n"
                    f"{specialist_text}"
                ),
            ),
        ]

        try:
            synth_resp, synthesis_backend, synthesis_model = await _call_coordinator_model(
                request_model=synthesizer,
                messages=synth_messages,
            )
            output_text = _assistant_text(synth_resp)
            synthesis = {
                "ok": True,
                "backend": synthesis_backend,
                "upstream_model": synthesis_model,
                "response": synth_resp,
                "output_text": output_text,
            }
            ok = True
            emit(
                {
                    "ts": now_unix(),
                    "type": "synthesis_completed",
                    "backend": synthesis_backend,
                    "upstream_model": synthesis_model,
                    "output_text": output_text,
                }
            )
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
            if not isinstance(detail, str):
                detail = _canonical_json(detail)
            error = detail
            synthesis = {"ok": False, "error": detail}
            emit(
                {
                    "ts": now_unix(),
                    "type": "synthesis_failed",
                    "error": detail,
                }
            )

    emit(
        {
            "ts": now_unix(),
            "type": "run_completed" if ok else "run_failed",
            "run_id": run_id,
            "ok": ok,
            "output_text": output_text,
            "error": error,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    )

    payload = {
        "run_id": run_id,
        "request_hash": request_hash,
        "ok": ok,
        "output_text": output_text,
        "error": error,
        "participants": participant_results,
        "synthesis": synthesis,
        "events": events,
    }
    try:
        _persist_run(run_id, payload)
    except Exception:
        pass
    return payload, synthesis_backend, synthesis_model
