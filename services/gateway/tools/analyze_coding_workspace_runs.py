#!/usr/bin/env python3
"""Summarize durable Nexus Coding Workspace run behavior."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable


def _tasks(path: Path) -> Iterable[Dict[str, Any]]:
    for item in sorted(path.glob("*.json")):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            yield payload


def analyze(tasks_dir: Path) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "tasks": 0,
        "total_runs": 0,
        "terminal_statuses": Counter(),
        "budget_exhausted": 0,
        "context_resets_per_run": {},
        "no_tool_call": 0,
        "no_tool_call_limit": 0,
        "finish_gate_failures": 0,
        "validation_failures": 0,
        "repeated_state_reads": 0,
        "repeated_git_diff": 0,
        "repeated_git_status": 0,
        "repeated_read_file": 0,
        "runs_missing_final_commit": 0,
        "runs_with_push": 0,
        "runs_with_pr": 0,
        "cycles_since_last_edit": [],
        "cycles_since_last_validation": [],
    }
    for task in _tasks(tasks_dir):
        report["tasks"] += 1
        events = [event for event in task.get("agent_events", []) if isinstance(event, dict)]
        runs = [run for run in task.get("agent_runs", []) if isinstance(run, dict)]
        report["total_runs"] += len(runs)
        for run in runs:
            report["terminal_statuses"][str(run.get("status") or "unknown")] += 1
            run_id = str(run.get("run_id") or "")
            reset_count = sum(1 for event in events if event.get("type") == "context_reset" and (not run_id or event.get("run_id") in {None, "", run_id}))
            report["context_resets_per_run"][run_id or f"unknown-{len(report['context_resets_per_run'])}"] = reset_count
        types = Counter(str(event.get("type") or "") for event in events)
        report["budget_exhausted"] += types["budget_exhausted"]
        report["no_tool_call"] += types["no_tool_call"]
        report["no_tool_call_limit"] += types["no_tool_call_limit"]
        report["finish_gate_failures"] += types["finish_gate"]
        report["repeated_state_reads"] += types["no_progress_guidance"] + types["no_progress_limit"]
        tool_names = [str(event.get("name") or "") for event in events if event.get("type") == "tool_finished"]
        report["repeated_git_diff"] += sum(1 for left, right in zip(tool_names, tool_names[1:]) if left == right == "coding_git_diff")
        report["repeated_git_status"] += sum(1 for left, right in zip(tool_names, tool_names[1:]) if left == right == "coding_git_status")
        report["repeated_read_file"] += sum(1 for left, right in zip(tool_names, tool_names[1:]) if left == right and left in {"coding_read_file", "coding_read_file_lines"})
        report["validation_failures"] += sum(
            1
            for event in events
            if event.get("type") == "tool_finished"
            and event.get("name") == "coding_run_command"
            and isinstance(event.get("result"), dict)
            and event["result"].get("ok") is False
        )
        terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
        if str(task.get("agent_status") or "") == "completed" and not terminal.get("final_commit"):
            report["runs_missing_final_commit"] += 1
        report["runs_with_push"] += int(bool(terminal.get("pushed_at")))
        report["runs_with_pr"] += int(bool(terminal.get("pr_url")))
        cycle = int(task.get("agent_cycle") or 0)
        edit_cycles = [int(event.get("cycle") or 0) for event in events if event.get("type") == "tool_finished" and event.get("name") in {"coding_write_file", "coding_replace_text", "coding_apply_patch"}]
        validation_cycles = [int(event.get("cycle") or 0) for event in events if event.get("type") == "tool_finished" and event.get("name") == "coding_run_command"]
        report["cycles_since_last_edit"].append(cycle - max(edit_cycles or [0]))
        report["cycles_since_last_validation"].append(cycle - max(validation_cycles or [0]))
    report["terminal_statuses"] = dict(report["terminal_statuses"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-dir", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = analyze(args.tasks_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
