from __future__ import annotations

import httpx

from app import httpx_client


def test_long_backend_timeout_keeps_read_budget_but_caps_connect_and_pool_waits():
    timeout = httpx_client._effective_timeout(600.0)

    assert timeout is not None
    assert timeout.read == 600.0
    assert timeout.write == 600.0
    assert timeout.connect == 30.0
    assert timeout.pool == 30.0


def test_short_backend_timeout_is_not_extended_by_caps():
    timeout = httpx_client._effective_timeout(10.0)

    assert timeout is not None
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.connect == 10.0
    assert timeout.pool == 10.0


def test_disabled_timeout_remains_disabled():
    assert httpx_client._effective_timeout(None) is None


def test_blank_read_timeout_gains_phase_and_limit():
    timeout = httpx_client._effective_timeout(600.0)
    exc = httpx.ReadTimeout("", request=httpx.Request("POST", "http://backend/v1/chat/completions"))

    httpx_client._ensure_request_error_text(exc, timeout=timeout)

    assert str(exc) == "ReadTimeout: read timeout after 600s"


def test_blank_connect_timeout_reports_capped_connect_limit():
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
