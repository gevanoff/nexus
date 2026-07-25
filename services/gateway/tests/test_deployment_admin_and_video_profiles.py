from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import resources_snapshot


REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
VIDEO_OPTIONS_PATH = REPO_ROOT / "services" / "media-generation" / "app" / "video_options.py"


def _load_video_options():
    spec = importlib.util.spec_from_file_location("nexus_video_options_test", VIDEO_OPTIONS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ltx_presets_are_two_stage_safe_and_include_720p() -> None:
    options = _load_video_options()

    assert options.LTX_RESOLUTION_PRESETS == {
        "480p": (704, 384),
        "540p": (768, 512),
        "720p": (1280, 704),
    }
    for width, height in options.LTX_RESOLUTION_PRESETS.values():
        assert width % 64 == 0
        assert height % 64 == 0
        assert max(width, height) <= options.LTX_MAX_EDGE
        assert width * height <= options.LTX_MAX_PIXELS


def test_ltx_symbolic_profile_overrides_gateway_injected_dimensions() -> None:
    options = _load_video_options()

    normalized = options.validate_video_payload(
        "ltx",
        {
            "prompt": "test",
            "resolution": "720p",
            # The generic Gateway normalizer can attach conventional dimensions
            # to advanced payloads. The backend profile remains authoritative.
            "width": 1280,
            "height": 720,
            "duration_seconds": 6,
        },
    )

    assert normalized["resolution"] == "720p"
    assert normalized["width"] == 1280
    assert normalized["height"] == 704


def test_ltx_validates_custom_dimensions_before_generation() -> None:
    options = _load_video_options()

    with pytest.raises(ValueError, match="divisible by 64"):
        options.validate_video_payload(
            "ltx",
            {"prompt": "test", "width": 704, "height": 416},
        )

    with pytest.raises(ValueError, match="positive integer"):
        options.validate_video_payload(
            "ltx",
            {"prompt": "test", "width": 704.5, "height": 384},
        )

    with pytest.raises(ValueError, match="safety limit"):
        options.validate_video_payload(
            "ltx",
            {"prompt": "test", "width": 1344, "height": 704},
        )

    normalized = options.validate_video_payload(
        "ltx",
        {"prompt": "test", "resolution": "704x384", "duration_seconds": 6},
    )
    assert normalized["resolution"] == "480p"
    assert normalized["width"] == 704
    assert normalized["height"] == 384


def test_video_timing_limits_are_rejected_instead_of_silently_truncated() -> None:
    options = _load_video_options()

    with pytest.raises(ValueError, match="above the 257-frame limit"):
        options.validate_video_payload(
            "ltx",
            {"prompt": "test", "resolution": "540p", "duration_seconds": 30, "fps": 24},
        )

    with pytest.raises(ValueError, match="above the 241-frame limit"):
        options.validate_video_payload(
            "hunyuan",
            {"prompt": "test", "resolution": "720p", "duration_seconds": 11, "fps": 24},
        )


def test_hunyuan_uses_symbolic_profile_and_drops_generic_dimensions() -> None:
    options = _load_video_options()

    normalized = options.validate_video_payload(
        "hunyuan",
        {
            "prompt": "test",
            "resolution": "720p",
            "width": 1280,
            "height": 720,
            "duration_seconds": 6,
        },
    )

    assert normalized["resolution"] == "720p"
    assert "width" not in normalized
    assert "height" not in normalized


def test_video_ui_profiles_match_backend_contract_and_duration_limits() -> None:
    html = (STATIC_ROOT / "video.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "video.js").read_text(encoding="utf-8")

    assert 'id="durationHint"' in html
    assert 'max="10"' in html
    assert "/static/video.js?v=3" in html
    assert "RESOLUTION_PROFILES" in js
    assert 'ltx_video: {' in js
    assert 'hunyuan_video: {' in js
    assert '{ value: "480p", label: "Low (704×384)", width: 704, height: 384 }' in js
    assert '{ value: "540p", label: "Standard (768×512)", width: 768, height: 512 }' in js
    assert (
        '{ value: "720p", label: "720p-class (1280×704, high memory)", '
        'width: 1280, height: 704 }'
    ) in js
    assert "maxDurationSeconds: 10" in js
    assert 'backendEl?.addEventListener("change", renderResolutionOptions)' in js


def test_deployment_admin_preserves_form_panel_and_submission_state() -> None:
    html = (STATIC_ROOT / "admin_deployments.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "admin_deployments.js").read_text(encoding="utf-8")

    assert 'id="jobsByHost"' in html
    assert "host-job-panel" in html
    assert "tbody tr.selected" in html
    assert "/static/admin_deployments.js?v=3" in html
    assert "captureFormState()" in js
    assert "state.form.components" in js
    assert "state.hostScroll" in js
    assert "captureHostScroll()" in js
    assert "restoreHostScroll()" in js
    assert "markSelectedJobRows()" in js
    assert "state.canSubmit" in js
    assert "submitButton.disabled = !state.canSubmit" in js
    assert "grouped.entries()" in js
    assert 'jobsByHost.querySelectorAll("tr[data-job-id]")' in js


def test_resources_use_topology_host_while_preserving_endpoint_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps(
            {
                "hosts": {
                    "ada2": {
                        "components": ["images"],
                        "optional_components": ["ltx-video"],
                    },
                    "stackrot": {"optional_components": ["hunyuan-video"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))

    record = SimpleNamespace(
        name="ltx-video",
        base_url="http://host.docker.internal:18180",
        hostname="host.docker.internal",
    )
    monkeypatch.setattr(
        resources_snapshot,
        "get_service_record_for_backend",
        lambda _backend_name, registry=None: record,
    )
    monkeypatch.setattr(
        resources_snapshot,
        "backend_hostname",
        lambda _backend_name, registry=None, fallback_base_url="": "host.docker.internal",
    )

    class Registry:
        @staticmethod
        def resolve_backend_class(value: str) -> str:
            return value

    location = resources_snapshot.backend_location_details(
        Registry(),
        "ltx_video",
        base_url=record.base_url,
    )

    assert location["host"] == "ada2"
    assert location["hostname"] == "ada2"
    assert location["deployment_host"] == "ada2"
    assert location["endpoint_host"] == "host.docker.internal"
    assert location["base_url"] == "http://host.docker.internal:18180"


def test_resources_do_not_guess_when_component_placement_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps(
            {
                "hosts": {
                    "ada2": {"optional_components": ["ltx-video"]},
                    "stackrot": {"optional_components": ["ltx-video"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))

    assert "ltx-video" not in resources_snapshot._topology_component_hosts()


def test_resources_merge_prefers_authoritative_deployment_host() -> None:
    lifecycle = {
        "backends": [
            {
                "backend_class": "ltx_video",
                "host": "host.docker.internal",
                "hostname": "host.docker.internal",
            }
        ]
    }
    registry = {
        "backends": [
            {
                "backend_class": "ltx_video",
                "deployment_host": "ada2",
                "endpoint_host": "host.docker.internal",
                "host": "ada2",
                "hostname": "ada2",
                "base_url": "http://host.docker.internal:18180",
            }
        ]
    }

    merged = resources_snapshot.merge_resources_payloads(lifecycle, registry)
    backend = merged["backends"][0]

    assert backend["host"] == "ada2"
    assert backend["hostname"] == "ada2"
    assert backend["deployment_host"] == "ada2"
    assert backend["endpoint_host"] == "host.docker.internal"
