from __future__ import annotations

import pytest

from app import coding_agent_guarded as guarded


@pytest.fixture(autouse=True)
def task_store(monkeypatch):
    monkeypatch.setattr(guarded._agent.cw, "load_task", lambda _task_id: {})


@pytest.mark.asyncio
async def test_backend_slot_is_released_when_post_acquire_logging_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Admission:
        def __init__(self) -> None:
            self.acquired = 0
            self.released = 0

        async def acquire(self, backend: str, route: str) -> None:
            assert backend == "local_vllm_fast"
            assert route == "chat"
            self.acquired += 1

        def release(self, backend: str, route: str) -> None:
            assert backend == "local_vllm_fast"
            assert route == "chat"
            self.released += 1

    admission = Admission()
    monkeypatch.setattr(guarded._agent, "get_admission_controller", lambda: admission)
    monkeypatch.setattr(
        guarded._agent,
        "_rank_coding_backend_candidates",
        lambda *_args, **_kwargs: [
            {
                "backend": "local_vllm_fast",
                "upstream_model": "devstral",
                "host": "stackrot",
                "ready": True,
                "available": 1,
                "limit": 1,
                "inflight": 0,
            }
        ],
    )
    monkeypatch.setattr(
        guarded._agent,
        "_append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event store unavailable")),
    )

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await guarded._acquire_backend_excluding(
            "coder",
            "local_mlx",
            "glm",
            task_id="code_test",
            cycle=2,
            attempt=1,
            excluded_backends={"local_mlx"},
        )

    assert admission.acquired == 1
    assert admission.released == 1


def test_toolless_review_does_not_require_tool_calling_backend() -> None:
    tool_free = type("Request", (), {"tools": None})()
    coding = type("Request", (), {"tools": [object()]})()

    assert guarded._request_requires_tool_calling(tool_free) is False
    assert guarded._request_requires_tool_calling(coding) is True


@pytest.mark.asyncio
async def test_backend_acquisition_propagates_tool_requirement(monkeypatch):
    ranking_calls = []

    class Admission:
        async def acquire(self, backend: str, route: str) -> None:
            assert (backend, route) == ("local_vllm_fast", "chat")

    def rank(*_args, **kwargs):
        ranking_calls.append(kwargs)
        return [{
            "backend": "local_vllm_fast",
            "upstream_model": "review-model",
            "ready": True,
            "available": 1,
        }]

    monkeypatch.setattr(guarded._agent, "get_admission_controller", lambda: Admission())
    monkeypatch.setattr(guarded._agent, "_rank_coding_backend_candidates", rank)

    selected = await guarded._acquire_backend_excluding(
        "coder",
        "local_vllm_fast",
        "review-model",
        task_id="code_test",
        cycle=1,
        attempt=0,
        excluded_backends=set(),
        require_tool_calling=False,
    )

    assert selected["backend"] == "local_vllm_fast"
    assert ranking_calls == [{"require_tool_calling": False}]
