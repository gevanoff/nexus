from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.config import S


def _report_dir() -> Path:
    return Path(str(getattr(S, "CODING_SMOKE_REPORT_DIR", "") or "/var/lib/gateway/coding_smoke_reports")).resolve()


def _load_report(path: Path) -> Dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("_file", str(path))
    try:
        payload.setdefault("_mtime", path.stat().st_mtime)
    except Exception:
        payload.setdefault("_mtime", 0)
    return payload


def _duration(report: Dict[str, Any]) -> int:
    explicit = report.get("duration_sec")
    try:
        value = int(float(explicit))
        if value >= 0:
            return value
    except Exception:
        pass
    try:
        started = int(float(report.get("started_at") or 0))
        finished = int(float(report.get("finished_at") or 0))
        if started > 0 and finished >= started:
            return finished - started
    except Exception:
        pass
    return 0


def _agent_payload(report: Dict[str, Any]) -> Dict[str, Any]:
    final_inspect = report.get("final_inspect")
    if isinstance(final_inspect, dict) and isinstance(final_inspect.get("agent"), dict):
        return final_inspect["agent"]
    return {}


def _summarize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    agent = _agent_payload(report)
    model = str(report.get("model") or agent.get("model") or "").strip()
    backend = str(report.get("backend") or agent.get("backend") or "").strip()
    upstream_model = str(report.get("upstream_model") or agent.get("upstream_model") or "").strip()
    profile_id = str(report.get("profile_id") or "fixture_median").strip() or "fixture_median"
    return {
        "ok": bool(report.get("ok")),
        "profile_id": profile_id,
        "profile_label": str(report.get("profile_label") or profile_id).strip() or profile_id,
        "complexity": str(report.get("complexity") or "simple").strip() or "simple",
        "model": model or "unknown",
        "backend": backend,
        "upstream_model": upstream_model,
        "task_id": str(report.get("task_id") or "").strip(),
        "branch_name": str(report.get("branch_name") or "").strip(),
        "started_at": int(float(report.get("started_at") or 0)),
        "finished_at": int(float(report.get("finished_at") or 0)),
        "duration_sec": _duration(report),
        "agent_elapsed_runtime_sec": int(float(report.get("agent_elapsed_runtime_sec") or agent.get("elapsed_runtime_sec") or 0)),
        "agent_status": str((report.get("final_task") or {}).get("agent_status") or agent.get("status") or "").strip(),
        "error": str(report.get("error") or (report.get("final_task") or {}).get("agent_error") or agent.get("error") or "").strip(),
        "changed_files": [str(item) for item in report.get("changed_files", []) if str(item)],
        "report_path": str(report.get("report_path") or report.get("_file") or "").strip(),
    }


def _metric_key(item: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(item.get("profile_id") or ""),
        str(item.get("model") or ""),
        str(item.get("backend") or ""),
        str(item.get("upstream_model") or ""),
    )


def _build_metrics(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for item in items:
        key = _metric_key(item)
        bucket = buckets.setdefault(
            key,
            {
                "profile_id": item.get("profile_id"),
                "profile_label": item.get("profile_label"),
                "complexity": item.get("complexity"),
                "model": item.get("model"),
                "backend": item.get("backend"),
                "upstream_model": item.get("upstream_model"),
                "runs": 0,
                "successes": 0,
                "failures": 0,
                "durations": [],
                "last_started_at": 0,
                "last_finished_at": 0,
                "last_ok": False,
                "last_duration_sec": 0,
                "last_error": "",
                "last_task_id": "",
            },
        )
        bucket["runs"] += 1
        if item.get("ok"):
            bucket["successes"] += 1
        else:
            bucket["failures"] += 1
        duration = int(item.get("duration_sec") or 0)
        if duration > 0:
            bucket["durations"].append(duration)
        finished = int(item.get("finished_at") or 0)
        if finished >= int(bucket.get("last_finished_at") or 0):
            bucket["last_started_at"] = item.get("started_at") or 0
            bucket["last_finished_at"] = finished
            bucket["last_ok"] = bool(item.get("ok"))
            bucket["last_duration_sec"] = duration
            bucket["last_error"] = item.get("error") or ""
            bucket["last_task_id"] = item.get("task_id") or ""

    out: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        durations = [int(value) for value in bucket.pop("durations", []) if int(value) > 0]
        bucket["success_rate"] = round(float(bucket["successes"]) / max(1, int(bucket["runs"])), 3)
        bucket["avg_duration_sec"] = int(round(sum(durations) / len(durations))) if durations else 0
        out.append(bucket)
    out.sort(key=lambda item: (str(item.get("profile_id") or ""), str(item.get("model") or ""), str(item.get("backend") or "")))
    return out


def payload(*, limit: int = 100) -> Dict[str, Any]:
    report_dir = _report_dir()
    cap = max(1, min(int(limit or 100), 500))
    raw_reports: List[Dict[str, Any]] = []
    if report_dir.exists():
        candidates = sorted(report_dir.glob("coding-smoke-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates[:cap]:
            report = _load_report(path)
            if report is not None:
                raw_reports.append(report)

    reports = [_summarize_report(report) for report in raw_reports]
    reports.sort(key=lambda item: int(item.get("finished_at") or item.get("started_at") or 0), reverse=True)
    latest = reports[0] if reports else None
    return {
        "ok": True,
        "generated_at": int(time.time()),
        "report_dir": str(report_dir),
        "report_count": len(reports),
        "latest": latest,
        "reports": reports,
        "metrics": _build_metrics(reports),
    }
