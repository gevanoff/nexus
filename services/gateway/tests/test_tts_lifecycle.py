import asyncio
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


@pytest.mark.asyncio
async def test_public_tts_releases_admission_if_start_notification_is_cancelled(monkeypatch):
    admission = _FakeAdmission()
    lifecycle = AsyncMock(side_effect=[asyncio.CancelledError(), None])

    monkeypatch.setattr(tts_routes, "check_capability", AsyncMock())
    monkeypatch.setattr(tts_routes, "ensure_tts_backend_ready", AsyncMock())
    monkeypatch.setattr(tts_routes, "get_admission_controller", lambda: admission)
    monkeypatch.setattr(tts_routes, "_notify_tts_lifecycle", lifecycle)
    generate = AsyncMock()
    monkeypatch.setattr(tts_routes, "generate_tts", generate)

    with pytest.raises(asyncio.CancelledError):
        await tts_routes._handle_tts(
            _FakeRequest({"text": "hello", "backend_class": "chatterbox_tts"})
        )

    assert admission.release_calls == [("chatterbox_tts", "tts")]
    assert lifecycle.await_args_list == [
        call("chatterbox_tts", "start"),
        call("chatterbox_tts", "finish"),
    ]
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_activation_is_serialized_per_backend(monkeypatch):
    tts_backend._TTS_ACTIVATION_LOCKS.clear()
    entered = 0
    max_entered = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def fake_locked(backend_class, *, reason, route_kind):
        nonlocal entered, max_entered, calls
        calls += 1
        entered += 1
        max_entered = max(max_entered, entered)
        try:
            if calls == 1:
                first_started.set()
                await release_first.wait()
            return {"backend_class": backend_class, "reason": reason, "route_kind": route_kind}
        finally:
            entered -= 1

    monkeypatch.setattr(tts_backend, "_ensure_tts_backend_ready_locked", fake_locked)

    first = asyncio.create_task(
        tts_backend.ensure_tts_backend_ready("chatterbox_tts", reason="first")
    )
    await first_started.wait()
    second = asyncio.create_task(
        tts_backend.ensure_tts_backend_ready("chatterbox_tts", reason="second")
    )
    await asyncio.sleep(0)

    assert calls == 1
    assert max_entered == 1

    release_first.set()
    await asyncio.gather(first, second)

    assert calls == 2
    assert max_entered == 1
