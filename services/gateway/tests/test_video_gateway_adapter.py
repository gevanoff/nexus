from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import ui_routes


def test_simple_video_ui_payload_normalizes_for_current_backend() -> None:
    payload = ui_routes._normalize_video_payload(
        {
            "prompt": "A lighthouse above stormy seas",
            "duration": 6,
            "resolution": "540P",
            "backend_class": "ltx_video",
        }
    )

    assert payload == {
        "prompt": "A lighthouse above stormy seas",
        "duration_seconds": 6,
        "resolution": "540p",
    }


def test_video_artifact_proxy_uses_current_backend_job_prefix() -> None:
    job_id = "ltx_" + ("a" * 32)
    payload = ui_routes._apply_video_artifact_proxy_urls(
        {"job_id": job_id, "videos": ["result.mp4"]},
        "ltx_video",
    )

    assert payload["url"] == (
        f"/ui/api/video/artifacts/ltx_video/{job_id}/result.mp4"
    )
    assert ui_routes._is_safe_video_job_id("ltx_video", job_id) is True
    assert ui_routes._is_safe_video_job_id(
        "ltx_video", "hunyuan_" + ("a" * 32)
    ) is False
