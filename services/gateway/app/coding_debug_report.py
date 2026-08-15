from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app import coding_forced_action as forced_action
from app import coding_workspace as cw
from app.config import S
from app.model_aliases import get_aliases


_REPORT_SCHEMA = "nexus_coding_debug_report.v1"
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY))\s*=\s*([^\s,;]+)"
)
_SECRET_TOKEN_RES = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/@\s]+@)", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+")
_SENSITIVE_KEYS = {
    "authorization",
    "body",
    "content",
    "file_content",
    "messages",
    "new_text",
    "old_text",
    "password",
    "patch",
    "private_key",
    "prompt",
    "raw",
    "request_body",
    "response_body",
    "secret",
    "system_prompt",
    "token",
}
_SAFE_EVENT_ARG_KEYS = {
    "argv",
    "case_sensitive",
    "check_only",
    "cwd",
    "extract_links",
    "fixed_strings",
    "glob",
    "include_html",
    "limit",
    "line_count",
    "max_bytes",
    "name",
    "path",
    "query",
    "remote",
    "start_line",
    "timeout_sec",
    "url",
}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def redact_text(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "")
    try:
        redactor = getattr(cw, "_redact_text", None)
        if callable(redactor):
            text = str(redactor(text) or "")
    except Exception:
        pass
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    for pattern in _SECRET_TOKEN_RES:
        text = pattern.sub("[REDACTED_TOKEN]", text)
    return _clip(text, limit)


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    lowered = str(key or "").strip().lower()
    if lowered in _SENSITIVE_KEYS or any(
        marker in lowered for marker in ("token", "secret", "password", "private_key", "authorization")
    ):
        return "[REDACTED]"
    if depth >= 6:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        output: Dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 80:
                output["_truncated_keys"] = True
                break
            output[str(child_key)] = _sanitize(child_value, key=str(child_key), depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        items = list(value)
        output = [_sanitize(item, depth=depth + 1) for item in items[:120]]
        if len(items) > 120:
            output.append("[TRUNCATED_ITEMS]")
        return output
    if isinstance(value, str):
        return redact_text(value, limit=4000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value, limit=1000)


def _event_view(event: Dict[str, Any]) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    for key in (
        "type",
        "ts",
        "cycle",
        "run_id",
        "name",
        "status",
        "summary",
        "error",
        "stop_reason_code",
        "backend",
        "model",
        "upstream_model",
    ):
        value = event.get(key)
        if value not in (None, ""):
            output[key] = redact_text(value, limit=1600) if isinstance(value, str) else value
    message = event.get("content")
    if message not in (None, ""):
        output["message"] = redact_text(message, limit=1600)
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    safe_args = {key: args.get(key) for key in _SAFE_EVENT_ARG_KEYS if key in args}
    if safe_args:
        output["args"] = _sanitize(safe_args)
    return output


def _run_view(run: Dict[str, Any]) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    for key in (
        "run_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "model",
        "backend",
        "upstream_model",
        "cycle",
        "max_cycles",
        "max_runtime_sec",
        "context_reset_cycles",
        "summary",
        "error",
        "stop_reason_code",
        "commit",
        "pr_number",
        "pr_url",
    ):
        value = run.get(key)
        if value not in (None, ""):
            output[key] = redact_text(value, limit=2000) if isinstance(value, str) else value
    return output


def _guidance_view(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "ts": message.get("ts"),
            "role": str(message.get("role") or ""),
            "actor": redact_text(message.get("actor"), limit=200),
            "run_id": str(message.get("run_id") or ""),
            "message": redact_text(message.get("content"), limit=1600),
        }.items()
        if value not in (None, "")
    }


def _terminal_view(terminal: Dict[str, Any]) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    for key in (
        "status",
        "success",
        "summary",
        "error",
        "stop_reason_code",
        "finished_at",
        "commit",
        "pr_number",
        "pr_url",
        "push_output",
        "pr_output",
    ):
        value = terminal.get(key)
        if value not in (None, ""):
            output[key] = redact_text(value, limit=2400) if isinstance(value, str) else value
    return output


def _safe_collect(name: str, callback) -> Dict[str, Any]:
    try:
        return {"ok": True, "value": callback()}
    except Exception as exc:
        return {
            "ok": False,
            "error": redact_text(f"{type(exc).__name__}: {exc}", limit=1200),
            "collector": name,
        }


def _git_snapshot(task_id: str) -> Dict[str, Any]:
    head_result = _safe_collect("git_head", lambda: cw.git_head(task_id))
    change_result = _safe_collect("git_change_summary", lambda: cw.git_change_summary(task_id))
    output: Dict[str, Any] = {"head": _sanitize(head_result)}
    changes = change_result.get("value") if change_result.get("ok") else None
    if isinstance(changes, dict):
        output["changes"] = {
            "ok": bool(changes.get("ok")),
            "counts": _sanitize(changes.get("counts") if isinstance(changes.get("counts"), dict) else {}),
            "files": _sanitize(changes.get("files") if isinstance(changes.get("files"), list) else []),
            "truncated": bool(changes.get("truncated")),
        }
    else:
        output["changes"] = _sanitize(change_result)
    return output


def _durable_state_view(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("ok"):
        return _sanitize(result)
    state = result.get("value") if isinstance(result.get("value"), dict) else {}
    output = {
        "ok": True,
        "schema": state.get("schema"),
        "generated_at": state.get("generated_at"),
        "branch": _sanitize(state.get("branch") if isinstance(state.get("branch"), dict) else {}),
        "progress": _sanitize(state.get("progress") if isinstance(state.get("progress"), dict) else {}),
        "changes": _sanitize(state.get("changes") if isinstance(state.get("changes"), dict) else {}),
        "validation": _sanitize(state.get("validation") if isinstance(state.get("validation"), dict) else {}),
        "diff_review": _sanitize(state.get("diff_review") if isinstance(state.get("diff_review"), dict) else {}),
        "blockers": _sanitize(state.get("blockers") if isinstance(state.get("blockers"), list) else []),
        "recent_guidance": [
            _guidance_view(item)
            for item in (state.get("recent_guidance") or [])
            if isinstance(item, dict)
        ],
        "recent_events": [
            _event_view(item)
            for item in (state.get("recent_events") or [])
            if isinstance(item, dict)
        ],
    }
    return _sanitize(output)


def _runtime_snapshot() -> Dict[str, Any]:
    names = (
        "CODING_AGENT_MAX_CYCLES_PER_RUN",
        "CODING_AGENT_MAX_RUNTIME_SEC",
        "CODING_AGENT_CONTEXT_RESET_CYCLES",
        "CODING_AGENT_CONTEXT_RESET_CHARS",
        "CODING_AGENT_MAX_TOKENS",
        "CODING_AGENT_TEXT_TOOL_MAX_TOKENS",
        "CODING_AGENT_MAX_NO_PROGRESS_CYCLES",
        "CODING_AGENT_BACKEND_RETRIES",
        "CODING_AGENT_BACKEND_RETRY_BASE_DELAY_SEC",
        "CODING_AGENT_BACKEND_RETRY_MAX_DELAY_SEC",
        "CODING_SEMANTIC_MEMORY_POLL_SEC",
        "CODING_SEMANTIC_MEMORY_STAGNANT_CYCLES",
        "CODING_COMMAND_TIMEOUT_SEC",
    )
    return {name: _sanitize(getattr(S, name, None), key=name) for name in names}


def _model_runtime_view(task: Dict[str, Any]) -> Dict[str, Any]:
    requested = str(task.get("agent_model") or task.get("coding_model") or "").strip()
    backend = str(task.get("agent_backend") or "").strip()
    upstream = str(task.get("agent_upstream_model") or "").strip()
    aliases = get_aliases()
    alias_name = requested.lower()
    alias = aliases.get(alias_name)

    def _matches(candidate: Any) -> bool:
        if candidate is None:
            return False
        candidate_backend = str(getattr(candidate, "backend", "") or "").strip()
        candidate_upstream = str(getattr(candidate, "upstream_model", "") or "").strip()
        if backend and candidate_backend and candidate_backend != backend:
            return False
        if upstream and candidate_upstream and candidate_upstream.lower() != upstream.lower():
            return False
        return bool(candidate_backend or candidate_upstream)

    if not _matches(alias) and (backend or upstream):
        for name, candidate in aliases.items():
            if getattr(candidate, "coding", None) is True and _matches(candidate):
                alias_name = str(name)
                alias = candidate
                break

    return {
        "requested_model": requested,
        "resolved_alias": alias_name if alias is not None else "",
        "backend": backend,
        "upstream_model": upstream,
        "alias_backend": str(getattr(alias, "backend", "") or "") if alias is not None else "",
        "alias_upstream_model": str(getattr(alias, "upstream_model", "") or "") if alias is not None else "",
        "context_window": getattr(alias, "context_window", None) if alias is not None else None,
        "max_input_tokens": getattr(alias, "max_input_tokens", None) if alias is not None else None,
        "max_tokens_cap": getattr(alias, "max_tokens_cap", None) if alias is not None else None,
        "coding_context_reset_tokens": getattr(alias, "coding_context_reset_tokens", None) if alias is not None else None,
        "effective_context_reset_tokens": task.get("agent_context_reset_tokens"),
        "context_reset_chars_fallback": task.get("agent_context_reset_chars_fallback"),
        "context_token_estimator": str(task.get("agent_context_token_estimator") or ""),
        "context_reset_route": _sanitize(task.get("agent_context_reset_route") if isinstance(task.get("agent_context_reset_route"), dict) else {}),
    }


def collect_debug_snapshot(task_id: str, *, active_runner: Optional[bool] = None) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    state_result = _safe_collect("coding_state_snapshot", lambda: cw.coding_state_snapshot(task_id))
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    runs = [item for item in (task.get("agent_runs") or []) if isinstance(item, dict)]
    guidance = [item for item in (task.get("guidance_messages") or []) if isinstance(item, dict)]
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    plan = cw.normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or ""))
    mission = cw.normalize_coding_mission(task)
    normalized_forced_action = forced_action.active_state(task)
    persisted_forced_action = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    repo_url = str(task.get("repo_url") or "")
    try:
        repo_url = cw.redact_repo_url(repo_url)
    except Exception:
        repo_url = redact_text(repo_url, limit=1000)

    snapshot: Dict[str, Any] = {
        "schema": _REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sharing_notice": (
            "Generated for debugging. Credentials and common secret formats are redacted; raw diffs, file contents, "
            "full tool payloads, and backend response bodies are intentionally omitted. Review before external sharing."
        ),
        "workspace": {
            "id": str(task.get("id") or task_id),
            "kind": str(task.get("kind") or "workspace"),
            "status": str(task.get("status") or ""),
            "repo_url": repo_url,
            "base_branch": str(task.get("base_branch") or ""),
            "branch_name": str(task.get("branch_name") or ""),
            "coding_model": str(task.get("coding_model") or ""),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "run_start_head": str(task.get("agent_start_head") or ""),
            "last_commit": str(task.get("last_commit") or ""),
            "last_checkpoint_commit": str(task.get("last_checkpoint_commit") or ""),
            "last_checkpoint_cycle": task.get("last_checkpoint_cycle"),
            "last_pushed_at": task.get("last_pushed_at"),
            "last_pr_at": task.get("last_pr_at"),
            "last_pr_output": redact_text(task.get("last_pr_output"), limit=1600),
            "task_brief": redact_text(task.get("prompt"), limit=2400),
        },
        "agent": {
            "active_runner": active_runner,
            "run_id": str(task.get("agent_run_id") or ""),
            "status": str(task.get("agent_status") or "idle"),
            "model": str(task.get("agent_model") or task.get("coding_model") or ""),
            "backend": str(task.get("agent_backend") or ""),
            "upstream_model": str(task.get("agent_upstream_model") or ""),
            "cycle": task.get("agent_cycle"),
            "max_cycles": task.get("agent_max_cycles"),
            "max_runtime_sec": task.get("agent_max_runtime_sec"),
            "context_reset_cycles": task.get("agent_context_reset_cycles"),
            "started_at": task.get("agent_started_at"),
            "finished_at": task.get("agent_finished_at"),
            "last_event_at": task.get("agent_last_event_at"),
            "pause_requested": bool(task.get("agent_pause_requested") or task.get("agent_stop_requested")),
            "summary": redact_text(task.get("agent_summary"), limit=2400),
            "error": redact_text(task.get("agent_error"), limit=2400),
            "stop_reason_code": str(task.get("agent_stop_reason_code") or terminal.get("stop_reason_code") or ""),
        },
        "controller": {
            "progress_state": _sanitize(task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}),
            "forced_action": _sanitize(normalized_forced_action),
            "forced_action_persisted": _sanitize(persisted_forced_action),
            "investigation_checkpoint": _sanitize(task.get("agent_investigation_checkpoint") if isinstance(task.get("agent_investigation_checkpoint"), dict) else {}),
            "investigation_checkpoint_error": redact_text(task.get("agent_investigation_checkpoint_error"), limit=1600),
            "integration_reconciliation": _sanitize(task.get("integration_reconciliation") if isinstance(task.get("integration_reconciliation"), dict) else {}),
            "metadata_error": _sanitize(task.get("metadata_error") if isinstance(task.get("metadata_error"), dict) else {}),
        },
        "model_runtime": _sanitize(_model_runtime_view(task)),
        "project_plan": _sanitize(plan),
        "mission": _sanitize(mission),
        "terminal_result": _terminal_view(terminal),
        "git": _git_snapshot(task_id),
        "durable_state": _durable_state_view(state_result),
        "runtime_config": _runtime_snapshot(),
        "recent_runs": [_run_view(item) for item in runs[-20:]],
        "recent_guidance": [_guidance_view(item) for item in guidance[-20:]],
        "recent_events": [_event_view(item) for item in events[-120:]],
    }
    return _sanitize(snapshot)


def _format_value(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _event_lines(events: Iterable[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for event in events:
        kind = str(event.get("type") or "event")
        cycle = event.get("cycle")
        name = str(event.get("name") or "")
        summary = str(event.get("summary") or event.get("message") or event.get("error") or "")
        prefix = f"cycle {cycle} · " if cycle not in (None, "") else ""
        target = f" · {name}" if name else ""
        lines.append(f"- {prefix}{kind}{target}: {_clip(summary, 500) if summary else '-'}")
    return lines or ["- No recorded events."]


def render_debug_report(snapshot: Dict[str, Any]) -> str:
    workspace = snapshot.get("workspace") if isinstance(snapshot.get("workspace"), dict) else {}
    agent = snapshot.get("agent") if isinstance(snapshot.get("agent"), dict) else {}
    controller = snapshot.get("controller") if isinstance(snapshot.get("controller"), dict) else {}
    model_runtime = snapshot.get("model_runtime") if isinstance(snapshot.get("model_runtime"), dict) else {}
    forced = controller.get("forced_action") if isinstance(controller.get("forced_action"), dict) else {}
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    changes = git.get("changes") if isinstance(git.get("changes"), dict) else {}
    counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
    files = changes.get("files") if isinstance(changes.get("files"), list) else []
    progress = controller.get("progress_state") if isinstance(controller.get("progress_state"), dict) else {}
    checkpoint = controller.get("investigation_checkpoint") if isinstance(controller.get("investigation_checkpoint"), dict) else {}
    events = snapshot.get("recent_events") if isinstance(snapshot.get("recent_events"), list) else []

    lines = [
        "# Nexus Coding Workspace Debug Report",
        "",
        f"Generated: {_format_value(snapshot.get('generated_at'))}",
        f"Schema: {_format_value(snapshot.get('schema'))}",
        "",
        f"> {snapshot.get('sharing_notice') or ''}",
        "",
        "## Workspace",
        "",
        f"- ID: `{_format_value(workspace.get('id'))}`",
        f"- Status: `{_format_value(workspace.get('status'))}`",
        f"- Repository: `{_format_value(workspace.get('repo_url'))}`",
        f"- Base / branch: `{_format_value(workspace.get('base_branch'))}` → `{_format_value(workspace.get('branch_name'))}`",
        f"- Model: `{_format_value(workspace.get('coding_model'))}`",
        f"- Run start head: `{_format_value(workspace.get('run_start_head'))}`",
        f"- Last commit: `{_format_value(workspace.get('last_commit') or workspace.get('last_checkpoint_commit'))}`",
        "",
        "## Agent and controller",
        "",
        f"- Agent status: `{_format_value(agent.get('status'))}`",
        f"- Live runner present: `{_format_value(agent.get('active_runner'))}`",
        f"- Run / cycle: `{_format_value(agent.get('run_id'))}` / `{_format_value(agent.get('cycle'))}`",
        f"- Backend / upstream model: `{_format_value(agent.get('backend'))}` / `{_format_value(agent.get('upstream_model'))}`",
        f"- Effective alias: `{_format_value(model_runtime.get('resolved_alias'))}`",
        f"- Context window / max input / output cap: `{_format_value(model_runtime.get('context_window'))}` / `{_format_value(model_runtime.get('max_input_tokens'))}` / `{_format_value(model_runtime.get('max_tokens_cap'))}`",
        f"- Effective context reset tokens: `{_format_value(model_runtime.get('effective_context_reset_tokens'))}`",
        f"- Stop reason: `{_format_value(agent.get('stop_reason_code'))}`",
        f"- Stagnant cycles: `{_format_value(progress.get('stagnant_cycles'))}`",
        f"- Investigation checkpoint cycle: `{_format_value(checkpoint.get('cycle'))}`",
        f"- Forced action: `{_format_value(forced.get('action_kind'))}` / canonical `{_format_value(forced.get('canonical_action_kind'))}`",
        f"- Forced tools: `{', '.join(str(item) for item in (forced.get('allowed_tools') or [])) or '-'}`",
        f"- Evidence / hypothesis: `{_format_value(forced.get('targeted_evidence_count'))}` / `{_format_value(forced.get('hypothesis_ready'))}`",
        "",
        "### Summary",
        "",
        redact_text(agent.get("summary"), limit=2400) or "-",
        "",
        "### Error",
        "",
        redact_text(agent.get("error"), limit=2400) or "-",
        "",
        "## Git state",
        "",
        (
            f"- Changed files: {int(counts.get('total') or 0)} "
            f"(added {int(counts.get('added') or 0)}, modified {int(counts.get('modified') or 0)}, "
            f"removed {int(counts.get('removed') or 0)}, untracked {int(counts.get('untracked') or 0)})"
        ),
    ]
    if files:
        lines.extend(f"- `{item.get('status') or '?'} {item.get('path') or ''}`" for item in files[:100] if isinstance(item, dict))
    else:
        lines.append("- No changed files reported.")
    lines.extend(
        [
            "",
            "## Recent event log",
            "",
            *_event_lines([item for item in events if isinstance(item, dict)]),
            "",
            "## Structured snapshot",
            "",
            "```json",
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def build_debug_report(task_id: str, *, active_runner: Optional[bool] = None) -> str:
    return render_debug_report(collect_debug_snapshot(task_id, active_runner=active_runner))


def report_filename(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id or "workspace")).strip("-") or "workspace"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"nexus-{safe}-debug-{stamp}.md"
