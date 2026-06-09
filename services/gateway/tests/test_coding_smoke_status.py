from __future__ import annotations

import json
import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_smoke_status


def test_coding_smoke_status_summarizes_reports_and_metrics(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    monkeypatch.setattr(coding_smoke_status.S, "CODING_SMOKE_REPORT_DIR", str(report_dir))

    report_a = {
        "ok": True,
        "profile_id": "fixture_median",
        "profile_label": "Fixture median repair",
        "complexity": "simple",
        "model": "coder",
        "backend": "local_mlx",
        "upstream_model": "mlx-community/MiniMax-M2.5-8bit",
        "task_id": "code_ok",
        "started_at": 100,
        "finished_at": 160,
        "duration_sec": 60,
        "changed_files": ["fixtures/coding-smoke-project/math_tools.py"],
    }
    report_b = {
        "ok": False,
        "profile_id": "fixture_median",
        "model": "coder",
        "backend": "local_mlx",
        "upstream_model": "mlx-community/MiniMax-M2.5-8bit",
        "task_id": "code_fail",
        "started_at": 200,
        "finished_at": 290,
        "error": "timeout",
    }
    (report_dir / "coding-smoke-a-code_ok.json").write_text(json.dumps(report_a), encoding="utf-8")
    (report_dir / "coding-smoke-b-code_fail.json").write_text(json.dumps(report_b), encoding="utf-8")

    payload = coding_smoke_status.payload(limit=10)

    assert payload["report_count"] == 2
    assert payload["latest"]["task_id"] == "code_fail"
    assert payload["latest"]["duration_sec"] == 90
    assert payload["metrics"][0]["runs"] == 2
    assert payload["metrics"][0]["successes"] == 1
    assert payload["metrics"][0]["failures"] == 1
    assert payload["metrics"][0]["success_rate"] == 0.5
