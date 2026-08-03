from __future__ import annotations

import ast
import json
import math
import re
import secrets
import time
from typing import Any


def now_unix() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def sse(data_obj: Any) -> bytes:
    return f"data: {json.dumps(data_obj, separators=(',', ':'))}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


class ThinkTagStreamParser:
    _START = "<think>"
    _END = "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ""

    @classmethod
    def _partial_suffix_len(cls, text: str) -> int:
        lower = text.lower()
        best = 0
        for token in (cls._START.lower(), cls._END.lower()):
            max_len = min(len(lower), len(token) - 1)
            for size in range(max_len, 0, -1):
                if lower.endswith(token[:size]):
                    best = max(best, size)
                    break
        return best

    def feed(self, text: str) -> tuple[str, str]:
        if not isinstance(text, str) or not text:
            return "", ""

        self._buffer += text
        visible_parts: list[str] = []
        hidden_parts: list[str] = []

        while self._buffer:
            lower = self._buffer.lower()

            if self._inside:
                end_idx = lower.find(self._END)
                if end_idx == -1:
                    keep = min(len(self._buffer), len(self._END) - 1)
                    hidden = self._buffer[:-keep] if keep else self._buffer
                    if hidden:
                        hidden_parts.append(hidden)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    break
                if end_idx > 0:
                    hidden_parts.append(self._buffer[:end_idx])
                self._buffer = self._buffer[end_idx + len(self._END) :]
                self._inside = False
                continue

            start_idx = lower.find(self._START)
            end_idx = lower.find(self._END)

            if end_idx != -1 and (start_idx == -1 or end_idx < start_idx):
                if end_idx > 0:
                    visible_parts.append(self._buffer[:end_idx])
                self._buffer = self._buffer[end_idx + len(self._END) :]
                continue

            if start_idx == -1:
                keep = self._partial_suffix_len(self._buffer)
                visible = self._buffer[:-keep] if keep else self._buffer
                if visible:
                    visible_parts.append(visible)
                self._buffer = self._buffer[-keep:] if keep else ""
                break

            if start_idx > 0:
                visible_parts.append(self._buffer[:start_idx])
            self._buffer = self._buffer[start_idx + len(self._START) :]
            self._inside = True

        return "".join(visible_parts), "".join(hidden_parts)

    def flush(self) -> tuple[str, str]:
        if self._inside:
            hidden = self._buffer
            self._buffer = ""
            self._inside = False
            return "", hidden
        tail = self._buffer.replace(self._START, "").replace(self._END, "")
        self._buffer = ""
        return tail, ""


def split_think_content(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or not text:
        return text, ""
    parser = ThinkTagStreamParser()
    visible, hidden = parser.feed(text)
    tail_visible, tail_hidden = parser.flush()
    return visible + tail_visible, hidden + tail_hidden


def _append_text_field(target: dict[str, Any], field: str, text: str) -> None:
    existing = target.get(field)
    if isinstance(existing, str) and existing:
        target[field] = existing + text
    else:
        target[field] = text


def _append_hidden_fields(target: dict[str, Any], text: str) -> None:
    if not text:
        return
    for field in ("thinking", "reasoning_content"):
        _append_text_field(target, field, text)


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BARE_TEXT_TOOL_CALL_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.-]{0,127})\s*(\{.*\})\s*$",
    re.DOTALL,
)
_MARKED_TEXT_TOOL_CALL_RE = re.compile(
    r"^.*?\[TOOL_CALLS\]\s*([A-Za-z_][A-Za-z0-9_.-]{0,127})\s*(.*)$",
    re.DOTALL,
)
_TEXT_TOOL_ARGUMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]{0,127})\s*:\s*(.*)$")
_WEB_SEARCH_TEXT_ALIASES = {"search_engine_query", "web_search_query"}


def allowed_tool_names_from_specs(tools: Any) -> set[str] | None:
    if not isinstance(tools, list):
        return None
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _normalize_allowed_tool_names(allowed_tool_names: Any) -> set[str] | None:
    if allowed_tool_names is None:
        return None
    if not isinstance(allowed_tool_names, (set, list, tuple)):
        return None
    out = {str(name).strip() for name in allowed_tool_names if str(name).strip()}
    return out


def _parse_text_tool_arguments(raw_args: str) -> dict[str, Any] | None:
    raw_args = raw_args.strip()
    if not raw_args:
        return {}
    if raw_args.startswith("{"):
        try:
            value = json.loads(raw_args)
        except Exception:
            return None
        return value if isinstance(value, dict) else None
    if raw_args.startswith("(") or ("=" in raw_args and "\n" not in raw_args):
        if raw_args.startswith("(") and not raw_args.endswith(")"):
            return None
        source = f"_tool{raw_args}" if raw_args.startswith("(") else f"_tool({raw_args})"
        try:
            expression = ast.parse(source, mode="eval").body
        except SyntaxError:
            return None
        if not isinstance(expression, ast.Call) or expression.args:
            return None
        args: dict[str, Any] = {}
        for keyword in expression.keywords:
            if keyword.arg is None or keyword.arg in args:
                return None
            try:
                args[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                return None
        return args
    args: dict[str, Any] = {}
    for raw_line in raw_args.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TEXT_TOOL_ARGUMENT_RE.match(line)
        if not match:
            return None
        key, raw_value = match.groups()
        raw_value = raw_value.strip()
        try:
            args[key] = json.loads(raw_value)
        except Exception:
            args[key] = raw_value
    return args


def extract_text_tool_call(content: Any, allowed_tool_names: Any) -> list[dict[str, Any]] | None:
    """Convert narrow local-model text tool syntax into one validated call."""
    if not isinstance(content, str) or not content.strip():
        return None
    match = _BARE_TEXT_TOOL_CALL_RE.match(content)
    if match:
        name, raw_args = match.groups()
    else:
        match = _MARKED_TEXT_TOOL_CALL_RE.match(content)
        if not match or "[TOOL_CALLS]" in match.group(2):
            return None
        name, raw_args = match.groups()
    args = _parse_text_tool_arguments(raw_args)
    if args is None:
        return None
    allowed = _normalize_allowed_tool_names(allowed_tool_names)
    name = name.strip()
    if allowed is not None and name not in allowed:
        if "web_search" in allowed and name in _WEB_SEARCH_TEXT_ALIASES:
            name = "web_search"
        else:
            return None
    if name == "web_search":
        if "limit" not in args and "max_results" in args:
            args["limit"] = args.pop("max_results")
        args.setdefault("limit", 5)
    return [
        {
            "id": "textcall1",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            },
        }
    ]


def tool_call_name_error(name: Any, allowed_tool_names: Any = None) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "missing tool name"
    normalized = name.strip()
    if normalized != name or not _TOOL_NAME_RE.fullmatch(normalized):
        return "malformed tool name"
    allowed = _normalize_allowed_tool_names(allowed_tool_names)
    if allowed is not None and normalized not in allowed:
        return "unknown tool name"
    return None


def _tool_name_snippet(name: Any, *, limit: int = 160) -> str:
    text = name if isinstance(name, str) else str(name)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tool_diagnostic(
    *,
    name: Any,
    reason: str,
    allowed_tool_names: set[str] | None,
    stream_index: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "reason": reason,
        "name": _tool_name_snippet(name),
    }
    if allowed_tool_names is not None:
        allowed = sorted(allowed_tool_names)
        out["allowed_tool_count"] = len(allowed)
        out["allowed_tool_names"] = allowed[:20]
    if stream_index is not None:
        out["stream_index"] = stream_index
    return out


class ToolCallValidationState:
    def __init__(self, allowed_tool_names: Any = None) -> None:
        self.allowed_tool_names = _normalize_allowed_tool_names(allowed_tool_names)
        self.invalid_stream_indexes: set[int] = set()
        self.valid_stream_indexes: set[int] = set()
        self.invalid_count = 0
        self.valid_count = 0
        self.notice_emitted = False

    @staticmethod
    def stream_index(tool_call: dict[str, Any]) -> int | None:
        try:
            return int(tool_call.get("index"))
        except Exception:
            return None

    def filter_stream_tool_calls(
        self,
        tool_calls: list[Any],
        *,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> list[Any]:
        out: list[Any] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                out.append(tool_call)
                continue
            index = self.stream_index(tool_call)
            if index is not None and index in self.invalid_stream_indexes:
                continue
            function = tool_call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name.strip():
                reason = tool_call_name_error(name, self.allowed_tool_names)
                if reason:
                    self.invalid_count += 1
                    if index is not None:
                        self.invalid_stream_indexes.add(index)
                    if diagnostics is not None:
                        diagnostics.append(
                            _tool_diagnostic(
                                name=name,
                                reason=reason,
                                allowed_tool_names=self.allowed_tool_names,
                                stream_index=index,
                            )
                        )
                    continue
                self.valid_count += 1
                if index is not None:
                    self.valid_stream_indexes.add(index)
            out.append(tool_call)
        return out


def openai_arguments_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except Exception:
        return str(value)


def normalize_tool_calls_for_openai(
    value: Any,
    *,
    stream_delta: bool = False,
    generate_missing_ids: bool = True,
) -> Any:
    if not isinstance(value, list):
        return value

    out: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            out.append(item)
            continue

        normalized: dict[str, Any] = {}
        if stream_delta and item.get("index") is not None:
            normalized["index"] = item.get("index")

        call_id = item.get("id") if item.get("id") is not None else item.get("toolCallId")
        if call_id is None:
            call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id.strip():
            normalized["id"] = call_id
        elif not stream_delta and generate_missing_ids:
            normalized["id"] = new_id("call")

        call_type = item.get("type")
        if isinstance(call_type, str) and call_type.strip():
            normalized["type"] = call_type
        else:
            normalized["type"] = "function"

        function = item.get("function")
        if isinstance(function, dict):
            normalized_function: dict[str, Any] = {}
            name = function.get("name")
            if name is None:
                name = item.get("name") if item.get("name") is not None else item.get("functionName")
            if name is None:
                name = item.get("tool_name")
            if isinstance(name, str) and name.strip():
                normalized_function["name"] = name.strip()
            if "arguments" in function:
                normalized_function["arguments"] = openai_arguments_string(function.get("arguments"))
            elif "arguments" in item:
                normalized_function["arguments"] = openai_arguments_string(item.get("arguments"))
            elif "args" in item:
                normalized_function["arguments"] = openai_arguments_string(item.get("args"))
            elif not stream_delta:
                normalized_function["arguments"] = ""
            if normalized_function:
                normalized["function"] = normalized_function
        else:
            normalized_function = {}
            name = item.get("name") if item.get("name") is not None else item.get("functionName")
            if name is None:
                name = item.get("tool_name")
            if isinstance(name, str) and name.strip():
                normalized_function["name"] = name.strip()
            if "arguments" in item:
                normalized_function["arguments"] = openai_arguments_string(item.get("arguments"))
            elif "args" in item:
                normalized_function["arguments"] = openai_arguments_string(item.get("args"))
            elif not stream_delta and normalized_function:
                normalized_function["arguments"] = ""
            if normalized_function:
                normalized["function"] = normalized_function
        out.append(normalized)
    return out


def _tool_parameter_schemas(tool_specs: Any) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    if not isinstance(tool_specs, list):
        return schemas
    for tool in tool_specs:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if isinstance(name, str) and name.strip() and isinstance(parameters, dict):
            schemas[name.strip()] = parameters
    return schemas


def _schema_types(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types = {raw_type}
    elif isinstance(raw_type, list):
        types = {str(item) for item in raw_type if isinstance(item, str)}
    else:
        types = set()
    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for variant in variants:
                types.update(_schema_types(variant))
    return types


def _coerce_string_for_schema(value: str, schema: Any) -> Any:
    types = _schema_types(schema)
    if not types or "string" in types:
        return value
    stripped = value.strip()
    if "integer" in types and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", stripped):
        try:
            return int(stripped)
        except Exception:
            pass
    if "number" in types:
        try:
            parsed_number = json.loads(stripped)
        except Exception:
            parsed_number = None
        if (
            isinstance(parsed_number, (int, float))
            and not isinstance(parsed_number, bool)
            and math.isfinite(parsed_number)
        ):
            return parsed_number
    if "boolean" in types and stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    if "null" in types and stripped == "null":
        return None
    if types.intersection({"array", "object"}):
        try:
            parsed_container = json.loads(stripped)
        except Exception:
            parsed_container = None
        if "array" in types and isinstance(parsed_container, list):
            return parsed_container
        if "object" in types and isinstance(parsed_container, dict):
            return parsed_container
    return value


def _coerce_value_for_schema(value: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    if isinstance(value, str):
        value = _coerce_string_for_schema(value, schema)
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {
                key: _coerce_value_for_schema(item, properties.get(key))
                for key, item in value.items()
            }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_coerce_value_for_schema(item, schema["items"]) for item in value]
    return value


def _coerce_tool_call_arguments_for_schemas(tool_calls: Any, tool_specs: Any) -> Any:
    schemas = _tool_parameter_schemas(tool_specs)
    if not schemas or not isinstance(tool_calls, list):
        return tool_calls
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        schema = schemas.get(str(function.get("name") or ""))
        raw_arguments = function.get("arguments")
        if not isinstance(schema, dict) or not isinstance(raw_arguments, str):
            continue
        try:
            arguments = json.loads(raw_arguments)
        except Exception:
            continue
        if not isinstance(arguments, dict):
            continue
        coerced = _coerce_value_for_schema(arguments, schema)
        if coerced != arguments:
            function["arguments"] = json.dumps(
                coerced,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
    return tool_calls


def _normalize_tool_calls(value: Any, *, stream_delta: bool) -> Any:
    return normalize_tool_calls_for_openai(value, stream_delta=stream_delta)


def _filter_nonstream_tool_calls(
    tool_calls: Any,
    *,
    allowed_tool_names: set[str] | None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[Any, int]:
    if not isinstance(tool_calls, list):
        return tool_calls, 0
    out: list[Any] = []
    dropped = 0
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            out.append(tool_call)
            continue
        function = tool_call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        reason = tool_call_name_error(name, allowed_tool_names)
        if reason:
            dropped += 1
            if diagnostics is not None:
                diagnostics.append(
                    _tool_diagnostic(
                        name=name,
                        reason=reason,
                        allowed_tool_names=allowed_tool_names,
                    )
                )
            continue
        out.append(tool_call)
    return out, dropped


def _invalid_tool_notice() -> str:
    return "Nexus suppressed an invalid backend tool call. Retry with a validated tool-calling model."


def sanitize_chat_choices(
    payload: Any,
    *,
    stream_parser: ThinkTagStreamParser | None = None,
    allowed_tool_names: Any = None,
    tool_specs: Any = None,
    tool_diagnostics: list[dict[str, Any]] | None = None,
    stream_tool_state: ToolCallValidationState | None = None,
) -> Any:
    if not isinstance(payload, dict):
        return payload

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload

    allowed = _normalize_allowed_tool_names(allowed_tool_names)

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        delta = choice.get("delta")
        if isinstance(delta, dict):
            if "tool_calls" in delta:
                before = delta.get("tool_calls")
                normalized_tool_calls = _normalize_tool_calls(before, stream_delta=True)
                normalized_tool_calls = _coerce_tool_call_arguments_for_schemas(
                    normalized_tool_calls,
                    tool_specs,
                )
                if stream_tool_state is not None and isinstance(normalized_tool_calls, list):
                    filtered = stream_tool_state.filter_stream_tool_calls(
                        normalized_tool_calls,
                        diagnostics=tool_diagnostics,
                    )
                    if filtered:
                        delta["tool_calls"] = filtered
                    else:
                        delta.pop("tool_calls", None)
                        if normalized_tool_calls and not stream_tool_state.notice_emitted:
                            _append_text_field(delta, "content", _invalid_tool_notice())
                            stream_tool_state.notice_emitted = True
                else:
                    delta["tool_calls"] = normalized_tool_calls
            content = delta.get("content")
            if isinstance(content, str):
                visible, hidden = stream_parser.feed(content) if stream_parser else split_think_content(content)
                delta["content"] = visible
                _append_hidden_fields(delta, hidden)

        message = choice.get("message")
        if isinstance(message, dict):
            if "tool_calls" not in message and allowed is not None:
                extracted = extract_text_tool_call(message.get("content"), allowed)
                if extracted:
                    message["tool_calls"] = extracted
                    message["content"] = None
            if "tool_calls" in message:
                message["tool_calls"] = _normalize_tool_calls(message.get("tool_calls"), stream_delta=False)
                message["tool_calls"] = _coerce_tool_call_arguments_for_schemas(
                    message.get("tool_calls"),
                    tool_specs,
                )
                message["tool_calls"], dropped = _filter_nonstream_tool_calls(
                    message.get("tool_calls"),
                    allowed_tool_names=allowed,
                    diagnostics=tool_diagnostics,
                )
                if not message.get("tool_calls"):
                    message.pop("tool_calls", None)
                    if dropped:
                        if not isinstance(message.get("content"), str) or not str(message.get("content") or "").strip():
                            message["content"] = _invalid_tool_notice()
                        if choice.get("finish_reason") in {None, "", "stop", "tool_calls"}:
                            choice["finish_reason"] = "stop"
                    continue
                if message.get("tool_calls") and choice.get("finish_reason") in {None, "", "stop"}:
                    choice["finish_reason"] = "tool_calls"
            content = message.get("content")
            if isinstance(content, str):
                visible, hidden = split_think_content(content)
                message["content"] = visible
                _append_hidden_fields(message, hidden)

    return payload
