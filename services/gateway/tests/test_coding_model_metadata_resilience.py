from __future__ import annotations

import os
from urllib import error as urlerror

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_model_metadata_resilience as resilience


def test_metadata_fetch_retries_dns_failure():
    calls = 0

    def original(model_id: str, *, timeout_sec: float = 10.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urlerror.URLError("Temporary failure in name resolution")
        return {"id": model_id}

    result = resilience.fetch_metadata_with_retry(
        original,
        "example/model",
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )

    assert result == {"id": "example/model"}
    assert calls == 2


def test_metadata_fetch_retries_server_error():
    calls = 0

    def original(model_id: str, *, timeout_sec: float = 10.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urlerror.HTTPError(
                "https://huggingface.co/api/models/example/model",
                503,
                "Service Unavailable",
                hdrs=None,
                fp=None,
            )
        return {"id": model_id}

    result = resilience.fetch_metadata_with_retry(
        original,
        "example/model",
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )

    assert result["id"] == "example/model"
    assert calls == 2


def test_metadata_fetch_does_not_retry_404():
    calls = 0

    def original(model_id: str, *, timeout_sec: float = 10.0):
        nonlocal calls
        calls += 1
        raise urlerror.HTTPError(
            "https://huggingface.co/api/models/example/missing",
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )

    with pytest.raises(urlerror.HTTPError):
        resilience.fetch_metadata_with_retry(
            original,
            "example/missing",
            sleep_fn=lambda _: None,
            attempts=3,
            base_delay_sec=0,
        )

    assert calls == 1
