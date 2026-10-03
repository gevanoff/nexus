import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.tool_calling import registry


def test_bing_results_parser_returns_bounded_structured_results():
    page = """
    <ol>
      <li class="b_algo"><h2><a href="https://example.com/a">First <strong>result</strong></a></h2><p>A &amp; B</p></li>
      <li class="b_algo"><h2><a href="javascript:alert(1)">Unsafe</a></h2></li>
      <li class="b_algo"><h2><a href="https://example.com/b">Second result</a></h2><p>More text</p></li>
    </ol>
    """

    results = registry._parse_bing_results(page, 1)

    assert results == [
        {"title": "First result", "url": "https://example.com/a", "snippet": "A & B"}
    ]


def test_bing_rss_parser_returns_safe_structured_results():
    feed = """
    <rss><channel>
      <item><title>First result</title><link>https://example.com/a</link><description>A &amp; B</description></item>
      <item><title>Unsafe</title><link>javascript:alert(1)</link><description>Skip</description></item>
      <item><title>Second result</title><link>https://example.com/b</link><description><![CDATA[More <b>text</b>]]></description></item>
    </channel></rss>
    """

    results = registry._parse_bing_rss_results(feed, 2)

    assert results == [
        {"title": "First result", "url": "https://example.com/a", "snippet": "A & B"},
        {"title": "Second result", "url": "https://example.com/b", "snippet": "More text"},
    ]


@pytest.mark.asyncio
async def test_web_search_uses_fixed_endpoint_and_parses_results(monkeypatch):
    captured = {}

    class FakeResponse:
        text = '<rss><channel><item><title>Example</title><link>https://example.com</link><description>Snippet</description></item></channel></rss>'

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, params):
            captured.update(url=url, params=params)
            return FakeResponse()

    monkeypatch.setattr(registry.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    result = await registry.builtin_tool_definitions()["web_search"].implementation(
        {"query": "current information", "limit": 3}
    )

    assert captured == {
        "url": "https://www.bing.com/search",
        "params": {"q": "current information", "format": "rss"},
    }
    assert result["ok"] is True
    assert result["provider"] == "bing"
    assert result["results"][0]["title"] == "Example"


@pytest.mark.asyncio
async def test_file_read_is_bounded_and_blocks_traversal(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(root))

    result = await registry.builtin_tool_definitions()["nexus_file_read"].implementation(
        {"path": "README.md", "start_line": 2, "end_line": 3, "max_chars": 100}
    )
    assert result["ok"] is True
    assert result["content"] == "two\nthree"

    with pytest.raises(ValueError, match="outside allowlisted"):
        await registry.builtin_tool_definitions()["nexus_file_read"].implementation(
            {"path": str(tmp_path / "outside.txt"), "start_line": None, "end_line": None, "max_chars": 100}
        )


def test_secret_redaction_covers_structured_and_text_values():
    redacted = registry.redact_secrets({"token": "abc", "line": "Authorization: Bearer-secret"})
    assert redacted["token"] == "[REDACTED]"
    assert "Bearer-secret" not in redacted["line"]


@pytest.mark.asyncio
async def test_file_grep_returns_structured_invalid_regex(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))

    result = await registry.builtin_tool_definitions()["nexus_file_grep"].implementation(
        {"root": ".", "pattern": "[", "glob": None, "limit": 10}
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_regex"


@pytest.mark.asyncio
async def test_file_grep_matches_glob_relative_to_selected_root(monkeypatch, tmp_path):
    wanted = tmp_path / "src"
    unwanted = tmp_path / "other" / "src"
    wanted.mkdir()
    unwanted.mkdir(parents=True)
    (wanted / "wanted.py").write_text("needle\n", encoding="utf-8")
    (unwanted / "unwanted.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))

    result = await registry.builtin_tool_definitions()["nexus_file_grep"].implementation(
        {"root": ".", "pattern": "needle", "glob": "src/*.py", "limit": 10}
    )

    assert result["ok"] is True
    assert [match["path"] for match in result["matches"]] == ["src/wanted.py"]


@pytest.mark.asyncio
async def test_file_stat_returns_bounded_metadata(monkeypatch, tmp_path):
    path = tmp_path / "README.md"
    path.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))

    result = await registry.builtin_tool_definitions()["nexus_file_stat"].implementation({"path": "README.md"})

    assert result["ok"] is True
    assert result["type"] == "file"
    assert result["size_bytes"] == path.stat().st_size
    assert result["modified_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_git_log_returns_structured_history(monkeypatch, tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))
    captured = {}

    def fake_git_log(command):
        captured["command"] = command
        return registry._git_log_result(
            "a" * 40 + "\x1f" + "a" * 7 + "\x1f2026-07-12T12:00:00+00:00\x1fNexus Test\x1fInitial commit\x1e",
            "",
            0,
        )

    monkeypatch.setattr(registry, "_run_git_log", fake_git_log)

    result = await registry.builtin_tool_definitions()["nexus_git_log"].implementation(
        {"repo": ".", "path": "README.md", "limit": 5}
    )

    assert result["ok"] is True
    assert len(result["commits"]) == 1
    assert result["commits"][0]["subject"] == "Initial commit"
    assert len(result["commits"][0]["commit"]) == 40
    assert "--max-count=5" in captured["command"]
    assert captured["command"][-2:] == ["--", "README.md"]



class _FakeTtsAdmission:
    def __init__(self, *, acquire_error: BaseException | None = None) -> None:
        self.acquire_error = acquire_error
        self.acquire_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []

    async def acquire(self, backend: str, capability: str) -> None:
        self.acquire_calls.append((backend, capability))
        if self.acquire_error is not None:
            raise self.acquire_error

    def release(self, backend: str, capability: str) -> None:
        self.release_calls.append((backend, capability))


def _patch_tts_tool_dependencies(monkeypatch, admission: _FakeTtsAdmission, generate_tts) -> AsyncMock:
    lifecycle = AsyncMock()
    monkeypatch.setattr(registry, "get_admission_controller", lambda: admission)
    monkeypatch.setattr(registry, "check_capability", AsyncMock())
    monkeypatch.setattr(registry, "ensure_tts_backend_ready", AsyncMock())
    monkeypatch.setattr(registry, "_notify_tts_lifecycle", lifecycle)
    monkeypatch.setattr(registry, "generate_tts", generate_tts)
    monkeypatch.setattr(
        registry,
        "save_audio_cache",
        lambda **_kwargs: ("a_test.wav", "abc123", "/tmp/a_test.wav"),
    )
    return lifecycle


@pytest.mark.asyncio
async def test_tts_generate_releases_once_and_balances_lifecycle_on_success(monkeypatch):
    admission = _FakeTtsAdmission()
    generate = AsyncMock(return_value=SimpleNamespace(audio=b"wav", content_type="audio/wav"))
    lifecycle = _patch_tts_tool_dependencies(monkeypatch, admission, generate)

    result = await registry._tts_generate({"text": "hello", "backend": "chatterbox_tts"})

    assert result["ok"] is True
    assert admission.acquire_calls == [("chatterbox_tts", "tts")]
    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]


@pytest.mark.asyncio
async def test_tts_generate_does_not_release_or_finish_when_acquire_fails(monkeypatch):
    admission = _FakeTtsAdmission(acquire_error=RuntimeError("capacity exhausted"))
    generate = AsyncMock()
    lifecycle = _patch_tts_tool_dependencies(monkeypatch, admission, generate)

    result = await registry._tts_generate({"text": "hello", "backend": "chatterbox_tts"})

    assert result["ok"] is False
    assert result["error"] == "tts_failed"
    assert admission.acquire_calls == [("chatterbox_tts", "tts")]
    assert admission.release_calls == []
    assert lifecycle.await_args_list == []
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_generate_releases_and_finishes_when_synthesis_fails(monkeypatch):
    admission = _FakeTtsAdmission()
    generate = AsyncMock(side_effect=RuntimeError("synthesis failed"))
    lifecycle = _patch_tts_tool_dependencies(monkeypatch, admission, generate)

    result = await registry._tts_generate({"text": "hello", "backend": "chatterbox_tts"})

    assert result["ok"] is False
    assert result["error"] == "tts_failed"
    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]


@pytest.mark.asyncio
async def test_tts_generate_releases_and_finishes_on_cancellation(monkeypatch):
    admission = _FakeTtsAdmission()
    generate = AsyncMock(side_effect=asyncio.CancelledError())
    lifecycle = _patch_tts_tool_dependencies(monkeypatch, admission, generate)

    with pytest.raises(asyncio.CancelledError):
        await registry._tts_generate({"text": "hello", "backend": "chatterbox_tts"})

    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]
