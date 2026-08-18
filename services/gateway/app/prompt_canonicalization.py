from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Tuple


_TOOL_USER_BRIDGE_CONTENT = "Tool result received."


def deterministic_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def canonicalize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonicalize_json_value(value[key]) for key in sorted(value.keys(), key=lambda item: str(item))}
    if isinstance(value, list):
        return [canonicalize_json_value(item) for item in value]
    return value


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def stable_sort_tools(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools
    normalized = [canonicalize_json_value(copy.deepcopy(tool)) for tool in tools]
    return sorted(normalized, key=lambda tool: (_tool_name(tool), deterministic_json_dumps(tool)))


def _assistant_message_has_payload(message: Any) -> bool:
    if not isinstance(message, dict):
        return True
    if str(message.get("role") or "").strip().lower() != "assistant":
        return True
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return True
    elif content not in (None, [], {}):
        return True
    tool_calls = message.get("tool_calls")
    if tool_calls is None:
        tool_calls = message.get("toolCalls")
    if isinstance(tool_calls, list) and tool_calls:
        return True
    function_call = message.get("function_call")
    if function_call is None:
        function_call = message.get("functionCall")
    return bool(function_call)


def _drop_structurally_empty_assistant_turns(messages: Any) -> Any:
    """Remove assistant turns that contain neither content nor a tool call.

    Some OpenAI-compatible backends reject an assistant message whose normalized
    content is empty and whose tool call is absent. Enforce that invariant at the
    final canonicalization boundary so a downstream compatibility shim cannot
    recreate an invalid turn after higher-level request materialization.
    """
    if not isinstance(messages, list):
        return messages
    return [message for message in messages if _assistant_message_has_payload(message)]


def _bridge_tool_to_user_turns(messages: Any) -> Any:
    """Insert a valid assistant boundary before a new user turn after tool output.

    Some OpenAI-compatible strict chat templates reject ``tool -> user`` even
    though Nexus can legitimately append controller/reroute guidance after a
    completed tool result. Preserve every original message and insert a minimal
    non-empty assistant acknowledgement. Never insert an empty assistant turn:
    strict vLLM templates such as Devstral reject that shape before generation.
    """
    if not isinstance(messages, list):
        return messages
    out: List[Any] = []
    for message in messages:
        if (
            isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() == "user"
            and out
            and isinstance(out[-1], dict)
            and str(out[-1].get("role") or "").strip().lower() == "tool"
        ):
            out.append({"role": "assistant", "content": _TOOL_USER_BRIDGE_CONTENT})
        out.append(message)
    return out


def canonicalize_chat_payload(payload: MutableMapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = canonicalize_json_value(dict(payload))
    if isinstance(out.get("messages"), list):
        messages = _drop_structurally_empty_assistant_turns(out.get("messages"))
        out["messages"] = _bridge_tool_to_user_turns(messages)
    if isinstance(out.get("tools"), list):
        out["tools"] = stable_sort_tools(out.get("tools"))
    return out


@dataclass(frozen=True)
class PromptPrefixFingerprint:
    prompt_prefix_hash: str
    prompt_prefix_chars: int


def _split_messages_for_prefix(messages: Any) -> Tuple[List[Any], Any]:
    if not isinstance(messages, list) or not messages:
        return [], None
    last = messages[-1]
    if isinstance(last, dict) and str(last.get("role") or "").strip().lower() == "user":
        return list(messages[:-1]), last
    return list(messages), None


def prompt_prefix_fingerprint(payload: MutableMapping[str, Any]) -> PromptPrefixFingerprint:
    canonical_payload = canonicalize_chat_payload(payload)
    prefix_messages, _current_user = _split_messages_for_prefix(canonical_payload.get("messages"))
    prefix_payload = {
        "model": canonical_payload.get("model"),
        "messages": prefix_messages,
        "tools": canonical_payload.get("tools") if isinstance(canonical_payload.get("tools"), list) else None,
        "tool_choice": canonical_payload.get("tool_choice"),
        "response_format": canonical_payload.get("response_format"),
        "chat_template_kwargs": canonical_payload.get("chat_template_kwargs"),
    }
    serialized = deterministic_json_dumps(prefix_payload)
    return PromptPrefixFingerprint(
        prompt_prefix_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        prompt_prefix_chars=len(serialized),
    )


@dataclass(frozen=True)
class PrefixObservation:
    key: str
    first_seen: float
    last_seen: float
    count: int
    prefix_chars: int


class PromptPrefixObservationCache:
    def __init__(self, max_entries: int = 2048) -> None:
        self._max_entries = max(64, int(max_entries or 2048))
        self._entries: OrderedDict[str, PrefixObservation] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, *, model: str, upstream: str, prompt_prefix_hash: str, prefix_chars: int) -> Dict[str, Any]:
        key = f"{model}|{upstream}|{prompt_prefix_hash}"
        now = time.time()
        with self._lock:
            existing = self._entries.get(key)
            if existing is None:
                updated = PrefixObservation(
                    key=key,
                    first_seen=now,
                    last_seen=now,
                    count=1,
                    prefix_chars=max(0, int(prefix_chars or 0)),
                )
                seen_before = False
            else:
                updated = PrefixObservation(
                    key=key,
                    first_seen=existing.first_seen,
                    last_seen=now,
                    count=existing.count + 1,
                    prefix_chars=existing.prefix_chars,
                )
                seen_before = True
            self._entries[key] = updated
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

        estimated_reused_prefix_chars = updated.prefix_chars if seen_before and updated.prefix_chars > 0 else 0
        return {
            "seen_before": seen_before,
            "count": updated.count,
            "cache_candidate": bool(seen_before and updated.prefix_chars > 0),
            "estimated_reused_prefix_chars": estimated_reused_prefix_chars,
        }


_PREFIX_CACHE: PromptPrefixObservationCache | None = None


def get_prefix_observation_cache(max_entries: int = 2048) -> PromptPrefixObservationCache:
    global _PREFIX_CACHE
    if _PREFIX_CACHE is None:
        _PREFIX_CACHE = PromptPrefixObservationCache(max_entries=max_entries)
    return _PREFIX_CACHE
