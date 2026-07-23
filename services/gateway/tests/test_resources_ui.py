from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import httpx

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import resources_snapshot


class ScriptedTelegramClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def get(self, _url):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def telegram_response(status: int, payload=None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        request=httpx.Request("GET", "https://api.telegram.org/bot-token/getMe"),
    )


def telegram_dns_error() -> httpx.ConnectError:
    return httpx.ConnectError(
        "[Errno -5] No address associated with hostname",
        request=httpx.Request("GET", "https://api.telegram.org/bot-token/getMe"),
    )


def test_resources_ui_hides_duplicate_core_services_section() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="control_plane_section"' in html
    assert 'id="core_services_section" class="resource-subsection" hidden' in html
    assert "/static/resources.js?v=22" in html
    assert "splitCoreServicesForResourceUi" in js
    assert "controlPlaneCoreServiceIds" in js
    assert "hideWhenEmpty: true" in js
    assert "No core services reported." not in js
    assert "telegramBridgeRuntimeIds" in js
    assert "visibleCoreServices" in js


def test_resources_ui_can_copy_individual_host_information() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="copy_hosts"' not in html
    assert "host-copy-button" in html
    assert "buildHostInfoText" in js
    assert "# Nexus Host Configuration:" in js
    assert "Hostname:" in js
    assert "OS:" in js
    assert "Platform:" in js
    assert "Processor:" in js
    assert "Memory:" in js
    assert "Network:" in js
    assert "network_interfaces" in js
    assert "fmtNetworkSpeed" in js
    assert "copyHostInfo(host, generatedAt)" in js
    assert 'copyButton.textContent = "Copy"' in js
    assert "navigator.clipboard.writeText" in js


def test_resources_merge_preserves_lifecycle_backend_host() -> None:
    lifecycle = {
        "backends": [
            {
                "backend_class": "local_vllm_fast",
                "host": "stackrot",
                "hostname": "stackrot",
            }
        ]
    }
    registry = {
        "backends": [
            {
                "backend_class": "local_vllm_fast",
                "host": "host.docker.internal",
                "hostname": "host.docker.internal",
                "base_url": "http://host.docker.internal:18001/v1",
            }
        ]
    }

    merged = resources_snapshot.merge_resources_payloads(lifecycle, registry)

    backend = merged["backends"][0]
    assert backend["host"] == "stackrot"
    assert backend["hostname"] == "stackrot"
    assert backend["base_url"] == "http://host.docker.internal:18001/v1"

    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    js = (static_root / "resources.js").read_text(encoding="utf-8")
    assert (
        "host: existing.host || existing.hostname || backend.hostname || backend.host"
        in js
    )


def test_resources_ui_shows_coding_smoke_health() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="coding_smoke_section"' in html
    assert 'id="coding_smoke"' in html
    assert "/ui/api/coding/smoke-status?limit=100" in js
    assert "renderCodingSmoke" in js
    assert "metrics-table" in js
    assert "metrics.slice(0, 48)" in js


def test_resources_ui_shows_model_integration_candidates() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="model_integrations_section"' in html
    assert "/ui/api/model-integrations" in js
    assert "renderModelIntegrations" in js
    assert "manual review required" in js


def test_resources_ui_shows_individual_telegram_bots() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")
    snapshot_source = Path(resources_snapshot.__file__).read_text(encoding="utf-8")

    assert 'id="telegram_bots_section"' in html
    assert 'id="telegram_bots"' in html
    assert "renderTelegramBots" in js
    assert "bot.bot_username" in js
    assert "bot.host" in js
    assert "bot.runtime" in js
    assert "runtime.containers" in js
    assert '"token_scope": "host_runtime"' in snapshot_source
    for expected in (
        "Cinder",
        "@CinderAshes_bot",
        "meltdown",
        "Hex",
        "@CrypticHex_bot",
        "stackrot",
        "Tess",
        "@Ms_Tess_bot",
        "ada2",
        "Clarion",
        "@Dr_Clarion_bot",
        "ai2",
    ):
        assert f'"{expected}"' in snapshot_source


def test_telegram_gateway_dependency_tracks_selected_backend(monkeypatch) -> None:
    class Registry:
        @staticmethod
        def resolve_backend_class(value: str) -> str:
            return value

    class Checker:
        def __init__(self, ready: bool) -> None:
            self.ready = ready

        def get_status(self, backend: str):
            assert backend == "local_vllm_fast"
            return SimpleNamespace(is_ready=self.ready, error="connection failed" if not self.ready else "")

    aliases = {"fast": SimpleNamespace(backend="local_vllm_fast")}
    monkeypatch.setenv("TELEGRAM_GATEWAY_MODEL", "fast")

    ready, note = resources_snapshot.telegram_gateway_dependency(Registry(), Checker(False), aliases)
    assert ready is False
    assert "local_vllm_fast is not ready" in note

    ready, note = resources_snapshot.telegram_gateway_dependency(Registry(), Checker(True), aliases)
    assert ready is True
    assert "local_vllm_fast is ready" in note


def test_telegram_probe_retries_transient_dns_failure(monkeypatch) -> None:
    resources_snapshot._TELEGRAM_PROBE_STATE.clear()
    monkeypatch.setenv("TELEGRAM_STATUS_PROBE_RETRIES", "2")
    monkeypatch.setenv("TELEGRAM_STATUS_PROBE_RETRY_DELAY_SEC", "0")
    client = ScriptedTelegramClient(
        [
            telegram_dns_error(),
            telegram_dns_error(),
            telegram_response(200, {"ok": True, "result": {"username": "Ms_Tess_bot"}}),
        ]
    )

    result = asyncio.run(
        resources_snapshot.telegram_get_me_probe(client, bot_id="tess", token="token")
    )

    assert client.calls == 3
    assert result["ok"] is True
    assert result["degraded"] is False
    assert result["api_username"] == "Ms_Tess_bot"


def test_telegram_probe_uses_bounded_last_known_good(monkeypatch) -> None:
    resources_snapshot._TELEGRAM_PROBE_STATE.clear()
    monkeypatch.setenv("TELEGRAM_STATUS_PROBE_RETRIES", "0")
    monkeypatch.setenv("TELEGRAM_STATUS_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("TELEGRAM_STATUS_LAST_GOOD_MAX_AGE_SEC", "300")

    good = ScriptedTelegramClient(
        [telegram_response(200, {"ok": True, "result": {"username": "CrypticHex_bot"}})]
    )
    first = asyncio.run(
        resources_snapshot.telegram_get_me_probe(good, bot_id="hex", token="token")
    )
    assert first["ok"] is True

    for expected_failures in (1, 2):
        failed = ScriptedTelegramClient([telegram_dns_error()])
        degraded = asyncio.run(
            resources_snapshot.telegram_get_me_probe(failed, bot_id="hex", token="token")
        )
        assert degraded["available"] is True
        assert degraded["degraded"] is True
        assert degraded["consecutive_failures"] == expected_failures
        assert degraded["api_username"] == "CrypticHex_bot"

    failed = ScriptedTelegramClient([telegram_dns_error()])
    unavailable = asyncio.run(
        resources_snapshot.telegram_get_me_probe(failed, bot_id="hex", token="token")
    )
    assert unavailable["available"] is False
    assert unavailable["degraded"] is False
    assert unavailable["consecutive_failures"] == 3


def test_telegram_probe_does_not_mask_authentication_error(monkeypatch) -> None:
    resources_snapshot._TELEGRAM_PROBE_STATE.clear()
    monkeypatch.setenv("TELEGRAM_STATUS_PROBE_RETRIES", "2")
    good = ScriptedTelegramClient(
        [telegram_response(200, {"ok": True, "result": {"username": "Dr_Clarion_bot"}})]
    )
    asyncio.run(resources_snapshot.telegram_get_me_probe(good, bot_id="clarion", token="token"))

    unauthorized = ScriptedTelegramClient([telegram_response(401, {"ok": False})])
    result = asyncio.run(
        resources_snapshot.telegram_get_me_probe(unauthorized, bot_id="clarion", token="token")
    )

    assert unauthorized.calls == 1
    assert result["available"] is False
    assert result["degraded"] is False
    assert "status 401" in result["error"]
