from __future__ import annotations

import pytest

from app import coding_agent_guarded as guarded


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
