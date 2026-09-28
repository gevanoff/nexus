from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_smoke_status


REPO_ROOT = Path(__file__).resolve().parents[3]


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
        "upstream_model": "mlx-community/MiniMax-M3-4bit",
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
        "upstream_model": "mlx-community/MiniMax-M3-4bit",
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


def test_ai2_runs_recurring_coding_smoke_suite_from_startup() -> None:
    topology = json.loads(
        (REPO_ROOT / "deploy" / "topology" / "production.json").read_text(
            encoding="utf-8"
        )
    )
    env = topology["hosts"]["ai2"]["env"]

    assert env["CODING_SMOKE_SCHEDULER_ENABLED"] == "true"
    assert env["CODING_SMOKE_RUN_AT_STARTUP"] == "true"
    assert env["CODING_SMOKE_START_INTERVAL_SEC"] == "3600"
    assert env["CODING_SMOKE_MODELS"] == "coder"
    assert env["CODING_SMOKE_PROFILES"] == (
        "fixture_median,fixture_inventory,fixture_route_flags"
    )
    assert env["CODING_SMOKE_TIMEOUT_SEC"] == "2400"
    assert env["CODING_SMOKE_STALLED_AFTER_SEC"] == "720"
    assert env["CODING_SMOKE_COMPLETION_GRACE_SEC"] == "60"
    assert env["CODING_SMOKE_PAUSE_SETTLE_SEC"] == "60"
