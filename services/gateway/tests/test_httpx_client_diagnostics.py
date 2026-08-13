from __future__ import annotations

import httpx
import pytest

from app import httpx_client


def test_long_backend_timeout_keeps_read_budget_and_divides_connect_budget_across_retries():
    timeout = httpx_client._effective_timeout(600.0, connect_retries=2)

    assert timeout is not None
    assert timeout.read == 600.0
    assert timeout.write == 600.0
    assert timeout.connect == 10.0
    assert timeout.pool == 30.0


def test_long_backend_timeout_without_transport_retries_allows_full_connect_budget():
    timeout = httpx_client._effective_timeout(600.0, connect_retries=0)

    assert timeout is not None
    assert timeout.connect == 30.0


def test_short_backend_timeout_is_not_extended_by_caps():
    timeout = httpx_client._effective_timeout(10.0)

    assert timeout is not None
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.connect == 10.0
    assert timeout.pool == 10.0
    assert httpx_client._client_timeout_value(10.0) == 10.0


def test_short_timeout_with_retries_still_respects_total_connect_budget():
    timeout = httpx_client._effective_timeout(9.0, connect_retries=2)

    assert timeout is not None
    assert timeout.read == 9.0
    assert timeout.connect == 3.0
    assert httpx_client._connect_budget_sec(9.0) == 9.0
    assert httpx_client._client_timeout_value(9.0) == 9.0


def test_disabled_timeout_remains_disabled():
    assert httpx_client._effective_timeout(None) is None
    assert httpx_client._connect_budget_sec(None) is None
    assert httpx_client._client_timeout_value(None) is None


def test_blank_read_timeout_gains_phase_and_limit():
    timeout = httpx_client._effective_timeout(600.0, connect_retries=2)
    exc = httpx.ReadTimeout("", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(exc, timeout=timeout)

    assert str(exc) == "ReadTimeout: read timeout after 600s"


def test_blank_connect_timeout_reports_per_attempt_and_aggregate_budget():
    timeout = httpx_client._effective_timeout(600.0, connect_retries=2)
    exc = httpx.ConnectTimeout("", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(
        exc,
        timeout=timeout,
        connect_attempts=3,
        connect_budget_sec=30.0,
    )

    assert str(exc) == "ConnectTimeout: connect timeout after 10s per attempt (30s budget across 3 attempts)"


def test_blank_connect_timeout_without_retries_reports_single_limit():
    timeout = httpx_client._effective_timeout(600.0)
    exc = httpx.ConnectTimeout("", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(exc, timeout=timeout)

    assert str(exc) == "ConnectTimeout: connect timeout after 30s"


def test_blank_non_timeout_request_error_keeps_exception_class():
    timeout = httpx_client._effective_timeout(600.0)
    exc = httpx.ConnectError("", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(exc, timeout=timeout)

    assert str(exc) == "ConnectError"


def test_existing_request_error_message_is_preserved():
    timeout = httpx_client._effective_timeout(600.0)
    exc = httpx.ReadTimeout("peer closed unexpectedly", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(exc, timeout=timeout)

    assert str(exc) == "peer closed unexpectedly"


@pytest.mark.asyncio
async def test_instrumented_send_applies_phase_timeouts_and_enriches_blank_timeout():
    request = httpx.Request("POST", "http://backend/v1/chat/completions")
    captured: dict[str, object] = {}

    class Client:
        async def send(self, sent_request, *args, **kwargs):
            captured["timeout"] = dict(sent_request.extensions.get("timeout") or {})
            raise httpx.ReadTimeout("", request=sent_request)

    client = Client()
    timeout = httpx_client._effective_timeout(600.0, connect_retries=2)
    httpx_client._instrument_client_send(
        client,
        timeout=timeout,
        connect_attempts=3,
        connect_budget_sec=30.0,
    )

    with pytest.raises(httpx.ReadTimeout) as exc:
        await client.send(request)

    assert captured["timeout"] == {
        "connect": 10.0,
        "read": 600.0,
        "write": 600.0,
        "pool": 30.0,
    }
    assert str(exc.value) == "ReadTimeout: read timeout after 600s"


@pytest.mark.asyncio
async def test_instrumented_send_preserves_explicit_request_timeout_extension():
    request = httpx.Request(
        "POST",
        "http://backend/v1/chat/completions",
        extensions={"timeout": {"connect": 1.0, "read": 2.0, "write": 2.0, "pool": 1.0}},
    )
    captured: dict[str, object] = {}

    class Client:
        async def send(self, sent_request, *args, **kwargs):
            captured["timeout"] = dict(sent_request.extensions.get("timeout") or {})
            return httpx.Response(200, request=sent_request)

    client = Client()
    timeout = httpx_client._effective_timeout(600.0, connect_retries=2)
    httpx_client._instrument_client_send(
        client,
        timeout=timeout,
        connect_attempts=3,
        connect_budget_sec=30.0,
    )

    response = await client.send(request)

    assert response.status_code == 200
    assert captured["timeout"] == {
        "connect": 1.0,
        "read": 2.0,
        "write": 2.0,
        "pool": 1.0,
    }
