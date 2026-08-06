from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from app import coding_workspace as workspace


SCHEMA = "nexus_coding_work_phase.v1"
DISCOVERY = "discovery"
DECISION = "decision"
EXECUTION = "execution"
_PHASES = {DISCOVERY, DECISION, EXECUTION}
_EDIT_TOOLS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DURATION_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:ms|s)(?!\w)")
_HEX_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_PROGRESS_PERCENT_RE = re.compile(r"\[\s*\d{1,3}%\]")
_TEMP_PATH_RE = re.compile(
    r"(?:(?:/private)?/tmp|/var/folders/[^\s:]+|[A-Za-z]:\\(?:Users\\[^\\\s]+\\)?AppData\\Local\\Temp)"
    r"(?:[/\\][^\s:]+)*"
)
_UV_RUN_LONG_OPTIONS_WITH_VALUES = {
    "--cache-dir",
    "--color",
    "--config-file",
    "--config-setting",
    "--config-settings",
    "--config-settings-package",
    "--constraint",
    "--default-index",
    "--directory",
    "--env-file",
    "--exclude-newer",
    "--exclude-newer-package",
    "--extra",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--group",
    "--index",
    "--index-strategy",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--no-group",
    "--only-group",
    "--override",
    "--package",
    "--prerelease",
    "--project",
    "--python",
    "--python-platform",
    "--python-version",
    "--refresh-package",
    "--reinstall-package",
    "--resolution",
    "--with",
    "--with-editable",
    "--with-requirements",
}
_UV_RUN_SHORT_OPTIONS_WITH_VALUES = {"-c", "-C", "-f", "-p"}
_UV_RUN_LONG_FLAG_OPTIONS = {
    "--active",
    "--all-extras",
    "--all-groups",
    "--compile-bytecode",
    "--exact",
    "--frozen",
    "--inexact",
    "--isolated",
    "--locked",
    "--managed-python",
    "--native-tls",
    "--no-cache",
    "--no-compile-bytecode",
    "--no-config",
    "--no-default-groups",
    "--no-dev",
    "--no-editable",
    "--no-managed-python",
    "--no-progress",
    "--no-project",
    "--no-python-downloads",
    "--no-sources",
    "--no-sync",
    "--offline",
    "--only-dev",
    "--refresh",
    "--reinstall",
}
_UV_RUN_SHORT_FLAG_OPTIONS = {"-q", "-v"}


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def mission_requires_file_changes(task: Mapping[str, Any]) -> bool:
    mission = task.get("mission") if isinstance(task.get("mission"), dict) else {}
    completion = mission.get("completion_policy") if isinstance(mission.get("completion_policy"), dict) else {}
    if "require_file_changes" in completion:
        return bool(completion.get("require_file_changes"))
    goal = str(mission.get("goal") or task.get("prompt") or task.get("agent_run_prompt") or "")
    return workspace.goal_expects_file_changes(goal)


def _stored_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw = task.get("agent_work_phase") if isinstance(task.get("agent_work_phase"), dict) else {}
    phase = str(raw.get("phase") or "").strip().lower()
    if str(raw.get("schema") or "") != SCHEMA or phase not in _PHASES:
        return {}
    return dict(raw)


def phase_state(
    task: Mapping[str, Any],
    *,
    phase: str = "",
    decision: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    stored = _stored_state(task)
    normalized = str(phase or stored.get("phase") or DISCOVERY).strip().lower()
    if normalized not in _PHASES:
        normalized = DISCOVERY
    return {
        "schema": SCHEMA,
        "phase": normalized,
        "decision": str(decision or stored.get("decision") or "").strip(),
        "reason": str(reason or stored.get("reason") or "").strip(),
    }


def current_phase(task: Mapping[str, Any]) -> str:
    return str(phase_state(task).get("phase") or DISCOVERY)


def _current_run_events(task: Mapping[str, Any]) -> list[Dict[str, Any]]:
    """Return only the current run segment, never an unbounded historical fallback."""
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    run_id = str(task.get("agent_run_id") or "").strip()
    if run_id:
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if str(event.get("type") or "") != "started":
                continue
            if str(event.get("run_id") or "").strip() == run_id:
                return events[index:]
        return []
    for index in range(len(events) - 1, -1, -1):
        if str(events[index].get("type") or "") == "started":
            return events[index:]
    return []


def _has_edit(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        str(event.get("type") or "") == "tool_started"
        and str(event.get("name") or "") in _EDIT_TOOLS
        for event in events
    )


def is_python_validation_command(parts: Sequence[str]) -> bool:
    if not parts:
        return False
    lowered = [item.lower() for item in parts]
    if "-m" in lowered:
        index = lowered.index("-m")
        module = lowered[index + 1] if index + 1 < len(lowered) else ""
        return module.split(".", 1)[0] in {"pytest", "unittest", "py_compile", "compileall", "ruff", "mypy"}
    script = Path(parts[0]).name.lower()
    return script.startswith("test_") and script.endswith(".py")


def _uv_run_nested_arguments(arguments: Sequence[str]) -> list[str]:
    """Strip recognized uv-run options without mistaking option values for the nested command."""
    items = [str(item).strip() for item in arguments if str(item).strip()]
    index = 0
    while index < len(items):
        token = items[index]
        lowered = token.lower()
        if token == "--":
            return items[index + 1 :]
        if not token.startswith("-") or token == "-":
            return items[index:]
        if "=" in token:
            index += 1
            continue
        matched_short = next(
            (option for option in _UV_RUN_SHORT_OPTIONS_WITH_VALUES if token == option or token.startswith(option)),
            "",
        )
        if matched_short:
            if token == matched_short:
                if index + 1 >= len(items):
                    return []
                index += 2
            else:
                index += 1
            continue
        if lowered in _UV_RUN_LONG_OPTIONS_WITH_VALUES:
            if index + 1 >= len(items):
                return []
            index += 2
            continue
        if lowered in _UV_RUN_LONG_FLAG_OPTIONS or token in _UV_RUN_SHORT_FLAG_OPTIONS:
            index += 1
            continue
        # Unknown standalone options may consume the next token. Evidence classification
        # must fail closed rather than treating a possible option value as the command.
        return []
    return []


def is_validation_command(argv: Any) -> bool:
    if not isinstance(argv, list) or not argv:
        return False
    parts = [str(item).strip() for item in argv if str(item).strip()]
    if not parts:
        return False
    command = Path(parts[0]).name.lower()
    lowered = [item.lower() for item in parts]
    if command in {"pytest", "ruff", "mypy"}:
        return True
    if command in {"python", "python3"}:
        return is_python_validation_command(parts[1:])
    if command == "node":
        return any(item in {"--check", "--test"} for item in lowered[1:])
    # Only explicit script/subcommand forms count; installs and adds must not mint progress.
    if command in {"npm", "pnpm", "yarn"}:
        arguments = [item for item in lowered[1:] if not item.startswith("-")]
        if not arguments:
            return False
        validation_scripts = {"test", "lint", "typecheck", "check", "build"}
        if arguments[0] in validation_scripts:
            return True
        return (
            arguments[0] in {"run", "run-script"}
            and len(arguments) > 1
            and (arguments[1] in validation_scripts or arguments[1].split(":", 1)[0] in validation_scripts)
        )
    if command == "uv":
        lowered_uv_arguments = [item.lower() for item in parts[1:]]
        if "run" not in lowered_uv_arguments:
            return False
        run_index = lowered_uv_arguments.index("run")
        arguments = _uv_run_nested_arguments(parts[run_index + 2 :])
        if not arguments:
            return False
        nested = Path(arguments[0]).name.lower()
        if nested in {"pytest", "ruff", "mypy"}:
            return True
        if nested in {"python", "python3"}:
            return is_python_validation_command(arguments[1:])
        return nested == "git" and [item.lower() for item in arguments[1:]] in (
            ["diff", "--check"],
            ["diff", "--cached", "--check"],
        )
    if command == "git":
        return lowered[1:] == ["diff", "--check"] or lowered[1:] == ["diff", "--cached", "--check"]
    return False


# Backward-compatible private aliases for existing callers and tests.
_python_validation_command = is_python_validation_command
_is_validation_command = is_validation_command


def advance_phase(
    task: Mapping[str, Any],
    *,
    stage: str,
    events: Sequence[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    prior = phase_state(task)
    phase = str(prior.get("phase") or DISCOVERY)
    event_list = list(events) if events is not None else _current_run_events(task)
    if _has_edit(event_list) or phase == EXECUTION:
        return phase_state(task, phase=EXECUTION, decision="remediate", reason="workspace execution has begun")
    enforced = str(stage or "observe") in {"interrupt", "recovery", "continuation"}
    if phase == DECISION:
        if mission_requires_file_changes(task):
            return phase_state(task, phase=EXECUTION, decision="remediate", reason="mission requires repository changes")
        return phase_state(task, phase=DECISION, decision="report_only", reason="review mission may complete without changes")
    if enforced:
        if mission_requires_file_changes(task):
            return phase_state(task, phase=EXECUTION, decision="remediate", reason="discovery produced a bounded remediation decision")
        return phase_state(task, phase=DECISION, decision="report_only", reason="discovery produced a bounded review decision")
    return phase_state(task, phase=DISCOVERY, decision="", reason="collect bounded repository evidence")


def _stored_evidence_fingerprint(task: Mapping[str, Any]) -> str:
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    observation = progress.get("observation") if isinstance(progress.get("observation"), dict) else {}
    return str(observation.get("evidence_fingerprint") or "")


def _normalize_validation_output(value: Any) -> str:
    text = _ANSI_ESCAPE_RE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    normalized: list[str] = []
    for raw_line in text.splitlines():
        line = _TIMESTAMP_RE.sub("<timestamp>", raw_line)
        line = _DURATION_RE.sub("<duration>", line)
        line = _HEX_ADDRESS_RE.sub("<address>", line)
        line = _PROGRESS_PERCENT_RE.sub("[<progress>]", line)
        line = _TEMP_PATH_RE.sub("<temp-path>", line)
        line = " ".join(line.split())
        if line:
            normalized.append(line)
    normalized.sort()
    return "\n".join(normalized)[:12000]


def discovery_evidence_fingerprint(task: Mapping[str, Any]) -> str:
    """Fingerprint completed validation outcomes without rewarding inspection churn.

    The latest stored fingerprint seeds a resumed run until that run produces a
    completed validation outcome. Output is normalized before hashing so
    elapsed-time, ordering, temporary-path, and timestamp noise cannot create
    artificial progress. Runtime guardrails decide when a changed fingerprint
    counts as progress, so phase-only transitions cannot retire forced action.
    """
    pending: Dict[str, Dict[str, Any]] = {}
    pending_without_id: list[Dict[str, Any]] = []
    signatures: set[str] = set()
    for event in _current_run_events(task):
        event_type = str(event.get("type") or "")
        name = str(event.get("name") or "")
        call_id = str(event.get("tool_call_id") or "").strip()
        if event_type == "tool_started" and name == "coding_run_command":
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if is_validation_command(args.get("argv")):
                if call_id:
                    pending[call_id] = dict(args)
                else:
                    pending_without_id.append(dict(args))
            continue
        if event_type != "tool_finished" or name != "coding_run_command":
            continue
        if call_id:
            args = pending.pop(call_id, None)
        else:
            args = pending_without_id.pop(0) if pending_without_id else None
        if args is None:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if str(result.get("error") or "") == "forced_action_tool_rejected":
            continue
        evidence = {
            "argv": args.get("argv") or [],
            "cwd": args.get("cwd") or "",
            "ok": result.get("ok"),
            "returncode": result.get("returncode"),
            "error": _normalize_validation_output(result.get("error")),
            "stdout": _normalize_validation_output(result.get("stdout")),
            "stderr": _normalize_validation_output(result.get("stderr")),
        }
        signatures.add("validation-result:" + _stable_hash(evidence))
    if signatures:
        return _stable_hash(sorted(signatures))
    return _stored_evidence_fingerprint(task)


def _review_scope_guidance() -> str:
    return (
        "Establish an authoritative review scope before broad inspection. First check whether the repository is shallow "
        "with `git rev-parse --is-shallow-repository`. If parent history is missing and you intend to review HEAD or the "
        "latest change, fetch enough history (for example `git fetch --deepen=1 origin`) before interpreting `git show`, "
        "`HEAD^`, or commit statistics; a shallow tip can look like a repository-wide root commit. Prefer an explicit "
        "base-to-head or parent-to-head changed-file list. If there is no branch delta and the mission is broader than one "
        "commit, state a bounded subsystem scope instead of implying that the whole repository was reviewed."
    )


def _review_evidence_guidance() -> str:
    return (
        "Use strict evidence labels. A confirmed defect requires a reproducible failing check or trace, or a logically "
        "complete source proof that includes the relevant call sites and contradicting paths. Source-reading concerns without "
        "that support are hypotheses, not confirmed defects. A missing-test claim requires a repository-wide search for "
        "semantically equivalent coverage, including adjacent and differently named test modules, and must name what was "
        "searched. Keep environment/configuration blockers separate. Assistant notes and working-memory summaries are leads, "
        "not evidence. When forced to finish before verification, downgrade the finding and state the one missing check."
    )


def phase_prompt(task: Mapping[str, Any]) -> str:
    state = phase_state(task)
    phase = str(state.get("phase") or DISCOVERY)
    review_only = not mission_requires_file_changes(task)
    if phase == DISCOVERY:
        prompt = (
            "Controller work phase: discovery. Gather bounded repository evidence, run targeted validation, "
            "and narrow findings; edits are optional until an actionable defect is supported."
        )
        if review_only:
            prompt += " " + _review_scope_guidance() + " " + _review_evidence_guidance()
        return prompt
    if phase == DECISION:
        prompt = (
            "Controller work phase: decision. Classify findings as confirmed actionable defects, one specified "
            "remaining check, environment/configuration blockers, or disproven; then report or remediate according to the mission."
        )
        if review_only:
            prompt += (
                " Before coding_finish, label every reported item as confirmed defect, hypothesis, missing-test gap, "
                "environment/configuration blocker, or disproven, and include the supporting command, trace, source proof, "
                "or repository-wide coverage search. "
                + _review_evidence_guidance()
            )
        return prompt
    return (
        "Controller work phase: execution. Make the smallest evidence-backed edit, run targeted validation, "
        "review the resulting diff, and finish; do not reopen broad repository exploration."
    )
