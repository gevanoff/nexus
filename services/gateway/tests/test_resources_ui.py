from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import resources_snapshot


def test_resources_ui_hides_duplicate_core_services_section() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="control_plane_section"' in html
    assert 'id="core_services_section" class="resource-subsection" hidden' in html
    assert "/static/resources.js?v=20" in html
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
    for expected in (
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
