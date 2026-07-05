from __future__ import annotations

from copy import deepcopy

from app.prompt_canonicalization import (
    PromptPrefixObservationCache,
    canonicalize_chat_payload,
    deterministic_json_dumps,
    prompt_prefix_fingerprint,
)


def test_deterministic_json_dumps_sorts_keys_stably():
    a = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    b = {"nested": {"x": 1, "y": 2}, "a": 1, "b": 2}

    assert deterministic_json_dumps(a) == deterministic_json_dumps(b)


def test_canonicalize_chat_payload_sorts_tools_without_mutating_input():
    payload = {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "zeta", "parameters": {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "alpha", "parameters": {"type": "object", "properties": {"d": {"type": "string"}, "c": {"type": "string"}}}}},
        ],
    }
    original = deepcopy(payload)

    out = canonicalize_chat_payload(payload)

    assert payload == original
    assert [tool["function"]["name"] for tool in out["tools"]] == ["alpha", "zeta"]
    assert list(out["tools"][0]["function"]["parameters"]["properties"].keys()) == ["c", "d"]


def test_prompt_prefix_fingerprint_stable_for_semantically_equivalent_payloads():
    base_messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "same final user turn"},
    ]
    p1 = {
        "model": "glm-5.2",
        "messages": base_messages,
        "tools": [
            {"type": "function", "function": {"name": "b", "parameters": {"type": "object", "properties": {"x": {"type": "number"}}}}},
            {"type": "function", "function": {"name": "a", "parameters": {"type": "object", "properties": {"y": {"type": "number"}}}}},
        ],
    }
    p2 = {
        "tools": [
            {"function": {"parameters": {"type": "object", "properties": {"y": {"type": "number"}}}, "name": "a"}, "type": "function"},
            {"function": {"parameters": {"type": "object", "properties": {"x": {"type": "number"}}}, "name": "b"}, "type": "function"},
        ],
        "messages": list(base_messages),
        "model": "glm-5.2",
    }

    f1 = prompt_prefix_fingerprint(canonicalize_chat_payload(p1))
    f2 = prompt_prefix_fingerprint(canonicalize_chat_payload(p2))

    assert f1.prompt_prefix_hash == f2.prompt_prefix_hash
    assert f1.prompt_prefix_chars == f2.prompt_prefix_chars


def test_prefix_observation_cache_reports_reuse_after_first_seen():
    cache = PromptPrefixObservationCache(max_entries=16)

    first = cache.observe(
        model="glm-5.2",
        upstream="local_mlx",
        prompt_prefix_hash="abc",
        prefix_chars=4096,
    )
    second = cache.observe(
        model="glm-5.2",
        upstream="local_mlx",
        prompt_prefix_hash="abc",
        prefix_chars=4096,
    )

    assert first["cache_candidate"] is False
    assert first["estimated_reused_prefix_chars"] == 0
    assert second["cache_candidate"] is True
    assert second["estimated_reused_prefix_chars"] == 4096
