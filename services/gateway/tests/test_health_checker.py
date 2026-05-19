from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import health_checker


def test_health_checker_debounces_transient_failures(monkeypatch):
    monkeypatch.setattr(health_checker.S, "HEALTH_CHECK_FAILURE_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(health_checker.S, "HEALTH_CHECK_FAILURE_GRACE_SEC", 60, raising=False)

    checker = health_checker.HealthChecker(check_interval=30, timeout=1)
    first = checker._record_status("local_mlx", is_healthy=False, is_ready=False, error="timeout", now=1000)
    second = checker._record_status("local_mlx", is_healthy=False, is_ready=False, error="timeout", now=1030)
    third = checker._record_status("local_mlx", is_healthy=False, is_ready=False, error="timeout", now=1060)

    assert first.is_ready is True
    assert second.is_ready is True
    assert second.suppressed_error == "timeout"
    assert third.is_ready is False
    assert third.error == "timeout"
    assert third.consecutive_failures == 3

    recovered = checker._record_status("local_mlx", is_healthy=True, is_ready=True, error=None, now=1070)

    assert recovered.is_ready is True
    assert recovered.consecutive_failures == 0
    assert recovered.suppressed_error is None
