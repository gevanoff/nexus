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

    assert options.LTX_RESOLUTION_PRESETS["540p"] == (768, 512)
    assert options.LTX_RESOLUTION_PRESETS["720p"] == (1280, 704)
    for width, height in options.LTX_RESOLUTION_PRESETS.values():
        assert width % 64 == 0
        assert height % 64 == 0


def test_ltx_rejects_invalid_custom_dimensions_before_generation() -> None:
    options = _load_video_options()

    with pytest.raises(ValueError, match="divisible by 64"):
        options.validate_video_payload(
            "ltx",
            {"prompt": "test", "width": 704, "height": 416},
        )

    normalized = options.validate_video_payload(
        "ltx",
        {"prompt": "test", "resolution": "720p"},
    )
    assert normalized["width"] == 1280
    assert normalized["height"] == 704


def test_video_ui_exposes_backend_specific_resolution_profiles() -> None:
    html = (STATIC_ROOT / "video.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "video.js").read_text(encoding="utf-8")

    assert '<option value="720p">720p</option>' in html
    assert "/static/video.js?v=2" in html
    assert "RESOLUTION_PROFILES" in js
    assert 'ltx_video: {' in js
    assert 'hunyuan_video: {' in js
    assert 'label: "Standard (768×512)"' in js
    assert 'label: "720p-class (1280×704)"' in js
    assert 'backendEl?.addEventListener("change", renderResolutionOptions)' in js


def test_deployment_admin_preserves_form_state_and_groups_jobs_by_host() -> None:
    html = (STATIC_ROOT / "admin_deployments.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "admin_deployments.js").read_text(encoding="utf-8")

    assert 'id="jobsByHost"' in html
    assert "host-job-panel" in html
    assert "/static/admin_deployments.js?v=2" in html
    assert "captureFormState()" in js
    assert "state.form.components" in js
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
