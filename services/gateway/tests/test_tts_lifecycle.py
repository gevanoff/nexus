import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import tts_backend, tts_routes


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return dict(self._body)


class _FakeAdmission:
    def __init__(self):
        self.acquire_calls = []
        self.release_calls = []

    async def acquire(self, backend, capability):
        self.acquire_calls.append((backend, capability))

    def release(self, backend, capability):
        self.release_calls.append((backend, capability))


@pytest.mark.asyncio
async def test_ensure_tts_backend_ready_waits_after_lifecycle_start(monkeypatch):
    plan = {
        "ok": True,
        "decision": "activate",
        "start": ["chatterbox_tts"],
        "stop": [],
        "backend": {"health_timeout_sec": 5},
    }
    statuses = [
        SimpleNamespace(raw_ready=False, raw_error="connection refused", error="not ready"),
        SimpleNamespace(raw_ready=True, raw_error=None, error=None),
    ]
    checker = SimpleNamespace(refresh_backend=AsyncMock(side_effect=statuses))

    monkeypatch.setattr(tts_backend, "lifecycle_manager_base_url", lambda: "http://lifecycle")
    lifecycle_call = AsyncMock(return_value=plan)
    monkeypatch.setattr(tts_backend, "call_lifecycle_manager", lifecycle_call)
    monkeypatch.setattr(tts_backend, "get_health_checker", lambda: checker)
    monkeypatch.setattr(tts_backend, "check_backend_ready", lambda *_args, **_kwargs: None)
    sleep = AsyncMock()
    monkeypatch.setattr(tts_backend.asyncio, "sleep", sleep)

    result = await tts_backend.ensure_tts_backend_ready(
        "chatterbox_tts",
        reason="test",
        route_kind="tts",
    )

    assert result == plan
    assert checker.refresh_backend.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_tts_balances_lifecycle_notifications(monkeypatch):
    admission = _FakeAdmission()
    lifecycle = AsyncMock()

    monkeypatch.setattr(tts_routes, "check_capability", AsyncMock())
    monkeypatch.setattr(tts_routes, "ensure_tts_backend_ready", AsyncMock())
    monkeypatch.setattr(tts_routes, "get_admission_controller", lambda: admission)
    monkeypatch.setattr(tts_routes, "_notify_tts_lifecycle", lifecycle)
    monkeypatch.setattr(
        tts_routes,
        "generate_tts",
        AsyncMock(
            return_value=tts_backend.TtsResult(
                kind="audio",
                content_type="audio/wav",
                audio=b"wav",
                gateway={"backend": "chatterbox_tts", "backend_class": "chatterbox_tts"},
            )
        ),
    )

    await tts_routes._handle_tts(
        _FakeRequest({"text": "hello", "backend_class": "chatterbox_tts"})
    )

    assert admission.acquire_calls == [("chatterbox_tts", "tts")]
    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]


@pytest.mark.asyncio
async def test_public_tts_finishes_lifecycle_on_synthesis_failure(monkeypatch):
    admission = _FakeAdmission()
    lifecycle = AsyncMock()

    monkeypatch.setattr(tts_routes, "check_capability", AsyncMock())
    monkeypatch.setattr(tts_routes, "ensure_tts_backend_ready", AsyncMock())
    monkeypatch.setattr(tts_routes, "get_admission_controller", lambda: admission)
    monkeypatch.setattr(tts_routes, "_notify_tts_lifecycle", lifecycle)
    monkeypatch.setattr(
        tts_routes,
        "generate_tts",
        AsyncMock(side_effect=RuntimeError("synthesis failed")),
    )

    with pytest.raises(HTTPException) as exc:
        await tts_routes._handle_tts(
            _FakeRequest({"text": "hello", "backend_class": "chatterbox_tts"})
        )

    assert exc.value.status_code == 502
    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]
