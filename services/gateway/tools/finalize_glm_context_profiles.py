from __future__ import annotations

import json
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


upstreams = Path("services/gateway/app/upstreams.py")
text = upstreams.read_text(encoding="utf-8")
old = '''def _request_alias_policy(
    req: ChatCompletionRequest,
    *,
    backend_name: str,
    model_name: str,
) -> Any:
    alias = get_alias(str(req.model or "").strip().lower())
    if alias is None:
        return None
    try:
        if not _alias_matches_backend(alias, backend_name=backend_name):
            return None
    except Exception:
        return None
    if str(alias.upstream_model or "").strip().lower() != str(model_name or "").strip().lower():
        return None
    return alias
'''
new = '''def _alias_matches_target(alias: Any, *, backend_name: str, model_name: str) -> bool:
    if alias is None:
        return False
    try:
        if not _alias_matches_backend(alias, backend_name=backend_name):
            return False
    except Exception:
        return False
    return (
        str(alias.upstream_model or "").strip().lower()
        == str(model_name or "").strip().lower()
    )


def _request_alias_policy(
    req: ChatCompletionRequest,
    *,
    backend_name: str,
    model_name: str,
) -> Any:
    requested_name = str(req.model or "").strip().lower()
    alias = get_alias(requested_name)
    if not _alias_matches_target(alias, backend_name=backend_name, model_name=model_name):
        return None

    alias_limit = getattr(alias, "max_input_tokens", None)
    if requested_name != "long" and isinstance(alias_limit, int) and alias_limit > 0:
        input_tokens = estimate_tokens(_mlx_glm_input_payload(req))
        if input_tokens > alias_limit:
            long_alias = get_alias("long")
            long_limit = getattr(long_alias, "max_input_tokens", None)
            if (
                _alias_matches_target(
                    long_alias,
                    backend_name=backend_name,
                    model_name=model_name,
                )
                and isinstance(long_limit, int)
                and input_tokens <= long_limit
            ):
                return long_alias
    return alias
'''
text = replace_once(text, old, new, "effective long alias policy")
upstreams.write_text(text, encoding="utf-8")

aliases_path = Path("services/gateway/app/model_aliases.json")
payload = json.loads(aliases_path.read_text(encoding="utf-8"))
long_alias = payload["aliases"]["long"]
long_alias.update(
    {
        "label": "GLM-5.2 Long 131K",
        "context_window": 131072,
        "max_input_tokens": 104000,
        "coding_context_reset_tokens": 94000,
        "max_tokens_cap": 24576,
    }
)
aliases_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

aliases_py = Path("services/gateway/app/model_aliases.py")
text = aliases_py.read_text(encoding="utf-8")
old = '''        "long": ModelAlias(
            backend=default_backend,
            upstream_model=default_strong_model,
            context_window=262_144 if mlx_context_policy else strong_context_window,
            tools=True,
            max_tokens_cap=32_768 if mlx_context_policy else None,
            max_input_tokens=210_000 if mlx_context_policy else None,
            coding_context_reset_tokens=185_000 if mlx_context_policy else None,
            thinking_enabled=True if mlx_context_policy else None,
'''
new = '''        "long": ModelAlias(
            backend=default_backend,
            upstream_model=default_strong_model,
            context_window=131_072 if mlx_context_policy else strong_context_window,
            tools=True,
            max_tokens_cap=24_576 if mlx_context_policy else None,
            max_input_tokens=104_000 if mlx_context_policy else None,
            coding_context_reset_tokens=94_000 if mlx_context_policy else None,
            thinking_enabled=True if mlx_context_policy else None,
'''
text = replace_once(text, old, new, "default long profile")
aliases_py.write_text(text, encoding="utf-8")

tests_path = Path("services/gateway/tests/test_glm_context_profiles.py")
text = tests_path.read_text(encoding="utf-8")
replacements = {
    '    assert aliases["long"]["context_window"] == 262_144\n':
        '    assert aliases["long"]["context_window"] == 131_072\n',
    '    assert aliases["long"]["max_input_tokens"] == 210_000\n':
        '    assert aliases["long"]["max_input_tokens"] == 104_000\n',
    '    assert aliases["long"]["coding_context_reset_tokens"] == 185_000\n':
        '    assert aliases["long"]["coding_context_reset_tokens"] == 94_000\n',
    '    assert aliases["long"]["max_tokens_cap"] == 32_768\n':
        '    assert aliases["long"]["max_tokens_cap"] == 24_576\n',
}
for old_text, new_text in replacements.items():
    text = replace_once(text, old_text, new_text, f"test contract {old_text.strip()}")
text += '''


def test_oversized_coder_request_uses_matching_long_policy(monkeypatch):
    coder = _glm_alias(max_input_tokens=100_000, max_tokens_cap=16_384)
    long_alias = _glm_alias(
        max_input_tokens=104_000,
        coding_context_reset_tokens=94_000,
        max_tokens_cap=24_576,
        thinking_enabled=True,
    )
    monkeypatch.setattr(
        upstreams,
        "get_alias",
        lambda name: {"coder": coder, "long": long_alias}.get(name),
    )
    monkeypatch.setattr(upstreams, "_alias_matches_backend", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(upstreams, "estimate_tokens", lambda _payload: 102_000)
    request = ChatCompletionRequest(
        model="coder",
        messages=[{"role": "user", "content": "large debugging context"}],
    )

    selected = upstreams._request_alias_policy(
        request,
        backend_name="local_mlx",
        model_name=coder.upstream_model,
    )

    assert selected is long_alias
'''
tests_path.write_text(text, encoding="utf-8")

user_tests = Path("services/gateway/tests/test_user_llm_settings.py")
text = user_tests.read_text(encoding="utf-8")
text = replace_once(
    text,
    '    assert aliases["long"]["context_window"] == 262144\n',
    '    assert aliases["long"]["context_window"] == 131072\n',
    "user settings long context",
)
user_tests.write_text(text, encoding="utf-8")

docs = Path("docs/GLM_CONTEXT_PROFILES.md")
text = docs.read_text(encoding="utf-8")
text = text.replace(
    "| `long` | Slow long-horizon work | 262,144 | 210,000 | 185,000 | 32,768 | enabled |",
    "| `long` | Maximum-headroom long-horizon work | 131,072 | 104,000 | 94,000 | 24,576 | enabled |",
)
text += '''

The deployed resident MLX lane is currently guaranteed at 131,072 tokens. The `long` profile therefore uses the largest verified runtime window with more input/output headroom than `coder`; it does not advertise 256K until a live MLX runtime profile and smoke test validate that size. Oversized `glm-chat` or `coder` requests that still fit the matching `long` budget inherit the long input, output, and thinking policy atomically.
'''
docs.write_text(text, encoding="utf-8")
