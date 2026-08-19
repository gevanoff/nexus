from __future__ import annotations

import contextvars
import re
from pathlib import PurePosixPath
from typing import Any, Dict, Mapping


_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9_.-]+)"
)
_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])"
)
_REPOSITORY_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".html", ".css", ".scss",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".sh",
}
_ACCEPTANCE_PARTS = {"tests", "test", "fixtures", "fixture", "examples", "example"}
_CONTEXT_SUFFIXES = {".md", ".rst", ".txt"}
_INVALID_TOOL_NOTICE = (
    "Nexus suppressed an invalid backend tool call. Retry with a validated tool-calling model."
)
_TOOL_DIAGNOSTICS: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "nexus_coding_tool_diagnostics",
    default=(),
)


def _hypothesis_pattern(labels: tuple[str, ...]) -> re.Pattern[str]:
    label_expr = "|".join(re.escape(label) for label in labels)
    return re.compile(
        rf"(?is)(?:^|[\n;]|(?<=[.!?])\s+)\s*(?:[-*]\s*)?(?:\*\*)?"
        rf"({label_expr})(?:\*\*)?\s*:\s*"
    )


def structured_hypothesis(
    base: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[bool, Dict[str, str]]:
    """Reference parser for the labelled hypothesis contract used by regression tests."""
    current_revision = base._plan_revision(task)
    try:
        activation_revision = int(state.get("activation_plan_revision", -1))
    except (TypeError, ValueError):
        activation_revision = -1
    if current_revision <= activation_revision:
        return False, {}

    labels = tuple(str(label) for label in base._HYPOTHESIS_FIELDS)
    text = base._project_plan_text(task)
    matches = list(_hypothesis_pattern(labels).finditer(text))
    if not matches:
        return False, {}

    fields: Dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = str(match.group(1) or "").strip()
        label = next(
            (candidate for candidate in labels if candidate.casefold() == raw_label.casefold()),
            "",
        )
        if not label or label in fields:
            continue
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else (
                trailing.start()
                if (trailing := base._TRAILING_PLAN_SECTION_RE.search(text, match.end()))
                else len(text)
            )
        )
        value = text[match.end() : end].strip(" \t\r\n;.")
        if len(value) < 8:
            return False, fields
        fields[label] = value
    return all(label in fields for label in labels), fields


def _normalized_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "://" in raw or re.match(r"^[A-Za-z]:/", raw):
        return ""
    parsed = PurePosixPath(raw)
    if ".." in parsed.parts:
        return ""
    return "/".join(part for part in parsed.parts if part not in {"", "."})


def _target_is_causal(target: str) -> bool:
    normalized = _normalized_path(target) if "/" in str(target or "") else str(target or "").strip()
    if not normalized:
        return False
    path = PurePosixPath(normalized)
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    stem = path.stem.casefold()
    conventional_test = (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or stem.endswith("_test")
        or stem.endswith("_spec")
    )
    if parts & _ACCEPTANCE_PARTS or conventional_test:
        return False
    if path.suffix.casefold() in _CONTEXT_SUFFIXES or "docs" in parts:
        return False
    return path.suffix.casefold() in _REPOSITORY_SUFFIXES


def _repository_paths(text: str) -> list[str]:
    out: list[str] = []
    for match in _PATH_RE.finditer(str(text or "")):
        path = _normalized_path(match.group(1))
        if path and path not in out:
            out.append(path)
    return out


def _repository_basenames(text: str) -> list[str]:
    out: list[str] = []
    for match in _FILENAME_RE.finditer(str(text or "")):
        name = str(match.group(1) or "").strip()
        if not name or "/" in name or not _target_is_causal(name):
            continue
        if name not in out:
            out.append(name)
    return out


def _resolve_asserted_targets(
    repository_evidence: str,
    state: Mapping[str, Any],
) -> list[str]:
    """Resolve hypothesis evidence while preserving full-path edit provenance."""
    verified = [
        _normalized_path(item)
        for item in (state.get("causal_evidence_targets") or [])
        if _normalized_path(item)
    ]
    candidates = [
        _normalized_path(item)
        for item in (state.get("candidate_causal_evidence_targets") or [])
        if _normalized_path(item)
    ]
    pool = [*verified, *candidates]
    asserted = [
        path for path in _repository_paths(repository_evidence) if _target_is_causal(path)
    ]

    for basename in _repository_basenames(repository_evidence):
        matches = sorted({path for path in pool if PurePosixPath(path).name == basename})
        target = matches[0] if len(matches) == 1 else basename
        if target not in asserted:
            asserted.append(target)
    return asserted


def refine_provenance_state(
    policy: Any,
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Open one corrective read only when the normal evidence transition is otherwise closed."""
    out = dict(state)
    if (
        str(out.get("action_kind") or "") != "evidence"
        or not out.get("evidence_provenance_enforced")
        or "coding_read_file_lines" in set(out.get("allowed_tools") or [])
    ):
        return out

    base = policy._base_policy(forced_action)
    hypothesis_ready, fields = base._structured_hypothesis(task, out)
    repository_evidence = str(fields.get("Repository evidence") or "")
    verified = {
        _normalized_path(item)
        for item in (out.get("causal_evidence_targets") or [])
        if _normalized_path(item)
    }
    if not hypothesis_ready or bool(out.get("hypothesis_causal_evidence_linked")):
        return out

    asserted = _resolve_asserted_targets(repository_evidence, out)
    unverified: list[str] = []
    for target in asserted:
        if "/" in target:
            if target not in verified:
                unverified.append(target)
        elif not any(PurePosixPath(path).name == target for path in verified):
            unverified.append(target)
    if not unverified:
        return out

    out["hypothesis_unverified_targets"] = unverified
    out["required_action"] = (
        "The structured hypothesis cites causal implementation/configuration evidence that has "
        "not been verified: "
        + ", ".join(unverified)
        + ". Read exactly one cited target with coding_read_file_lines, or revise Repository "
        "evidence to cite one of the already verified causal targets."
    )
    out["allowed_tools"] = sorted(
        {"coding_read_file_lines", "coding_update_plan", "coding_finish"}
    )
    return out


def provenance_prompt_context(base: Any, state: Mapping[str, Any]) -> str:
    allowed = ", ".join(state.get("allowed_tools") or [])
    candidates = [
        _normalized_path(item)
        for item in (state.get("candidate_causal_evidence_targets") or [])
        if _normalized_path(item)
    ]
    verified = [
        _normalized_path(item)
        for item in (state.get("causal_evidence_targets") or [])
        if _normalized_path(item)
    ]
    unverified = [
        str(item)
        for item in (state.get("hypothesis_unverified_targets") or [])
        if str(item).strip()
    ]
    fields = "\n".join(
        f"{label}: <specific finding>" for label in base._HYPOTHESIS_FIELDS
    )

    if unverified:
        next_step = (
            "The current hypothesis cites an unverified causal target. Read exactly one of "
            "these cited targets and no unrelated file: "
            + ", ".join(unverified)
            + ". After that read, revise the plan if needed so Repository evidence cites the "
            "full verified repository-relative path."
        )
    elif verified:
        next_step = (
            "Verified causal implementation/configuration targets are: "
            + ", ".join(verified)
            + ". Do not reconstruct or infer a different target from compacted model notes. "
            "Call coding_update_plan and make Repository evidence cite at least one of those "
            "exact full paths."
        )
    elif candidates:
        next_step = (
            "Candidate causal targets are: "
            + ", ".join(candidates)
            + ". Search location alone does not establish root cause. Read exactly one "
            "candidate with coding_read_file_lines before forming the hypothesis."
        )
    else:
        next_step = (
            "Use one bounded coding_search_text or coding_read_file_lines action against "
            "implementation/configuration. Tests, fixtures, examples, and documentation may "
            "define acceptance criteria but do not establish root cause."
        )

    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state, but editing "
        "is not yet authorized. The execution policy applies an explicit causal-evidence "
        "provenance gate. "
        + next_step
        + " When recording the remediation hypothesis, put each contract field on its own "
        "labelled line (equivalent sentence-separated labels are also accepted):\n"
        + fields
        + f"\nAvailable tools: {allowed or 'coding_finish'}."
    )


def _safe_tool_diagnostics(items: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(items, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in items[:5]:
        if not isinstance(item, Mapping):
            continue
        allowed = (
            item.get("allowed_tool_names")
            if isinstance(item.get("allowed_tool_names"), list)
            else []
        )
        out.append(
            {
                "reason": str(item.get("reason") or "")[:120],
                "name": str(item.get("name") or "")[:160],
                "allowed_tool_names": [str(name)[:128] for name in allowed[:20]],
            }
        )
    return tuple(out)


def _request_policy_allowed_tools(request: Any) -> list[str]:
    x_nexus = (
        request.get("x_nexus")
        if isinstance(request, Mapping)
        else getattr(request, "x_nexus", None)
    )
    if not isinstance(x_nexus, Mapping):
        return []
    policy = x_nexus.get("coding_execution_policy")
    if not isinstance(policy, Mapping):
        return []
    raw = policy.get("allowed_tools")
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(name).strip() for name in raw if str(name).strip()})[:20]


def _diagnostics_with_policy_tools(
    diagnostics: tuple[dict[str, Any], ...],
    request: Any,
) -> tuple[dict[str, Any], ...]:
    authorized = _request_policy_allowed_tools(request)
    if not authorized:
        return diagnostics
    out: list[dict[str, Any]] = []
    for item in diagnostics:
        enriched = dict(item)
        enriched["allowed_tool_names"] = authorized
        out.append(enriched)
    return tuple(out)


def diagnostic_notice(item: Mapping[str, Any]) -> str:
    name = str(item.get("name") or "(missing tool name)").strip()[:160]
    reason = str(item.get("reason") or "invalid tool call").strip()[:120]
    allowed = [
        str(value)
        for value in (item.get("allowed_tool_names") or [])
        if str(value).strip()
    ]
    suffix = ", ".join(allowed) if allowed else "not available in transport diagnostic"
    return (
        f"Nexus suppressed backend tool call {name!r}: {reason}. "
        f"Currently authorized Coding Workspace tools: {suffix}."
    )


def _install_invalid_tool_diagnostics(agent: Any) -> None:
    from app import upstreams

    if not bool(getattr(upstreams, "_coding_contract_tool_diagnostics_installed", False)):
        original_log = upstreams._log_invalid_response_tool_calls

        def log_with_capture(
            diagnostics: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            _TOOL_DIAGNOSTICS.set(_safe_tool_diagnostics(diagnostics))
            original_log(diagnostics, **kwargs)

        upstreams._log_invalid_response_tool_calls = log_with_capture
        upstreams._coding_contract_tool_diagnostics_installed = True

    if bool(getattr(agent, "_coding_contract_call_backend_diagnostics_installed", False)):
        return
    original_call = agent.call_backend_chat

    async def call_with_diagnostics(*args: Any, **kwargs: Any) -> Any:
        token = _TOOL_DIAGNOSTICS.set(())
        try:
            response = await original_call(*args, **kwargs)
            diagnostics = _TOOL_DIAGNOSTICS.get()
        finally:
            _TOOL_DIAGNOSTICS.reset(token)
        if not diagnostics or not isinstance(response, dict):
            return response

        request = args[0] if args else kwargs.get("req")
        diagnostics = _diagnostics_with_policy_tools(diagnostics, request)
        output = dict(response)
        gateway = (
            dict(output.get("_gateway") or {})
            if isinstance(output.get("_gateway"), dict)
            else {}
        )
        gateway["coding_tool_call_diagnostics"] = [dict(item) for item in diagnostics]
        output["_gateway"] = gateway

        choices = output.get("choices")
        if isinstance(choices, list):
            copied_choices: list[Any] = []
            for choice in choices:
                if not isinstance(choice, dict):
                    copied_choices.append(choice)
                    continue
                copied = dict(choice)
                message = copied.get("message")
                if isinstance(message, dict):
                    copied_message = dict(message)
                    if (
                        str(copied_message.get("content") or "").strip()
                        == _INVALID_TOOL_NOTICE
                    ):
                        copied_message["content"] = diagnostic_notice(diagnostics[0])
                    copied["message"] = copied_message
                copied_choices.append(copied)
            output["choices"] = copied_choices
        return output

    agent.call_backend_chat = call_with_diagnostics
    agent._coding_contract_call_backend_diagnostics_installed = True


def _install_blocked_finish_audit(agent: Any) -> None:
    if bool(getattr(agent, "_coding_contract_blocked_finish_audit_installed", False)):
        return
    original = agent._no_change_audit

    def audit_with_blocker_semantics(**kwargs: Any):
        finish_called = bool(kwargs.get("finish_called"))
        finish_success = bool(kwargs.get("finish_success"))
        committed = bool(kwargs.get("committed_changes"))
        uncommitted = bool(kwargs.get("uncommitted_changes"))
        if finish_called and not finish_success and not committed and not uncommitted:
            finish_summary = str(kwargs.get("finish_summary") or "").strip()
            summary = (
                finish_summary
                or "The coding agent reported a concrete blocker without modifying the workspace."
            )
            return (
                False,
                summary,
                {
                    "type": "blocked_finish",
                    "ok": False,
                    "summary": (
                        "The coding agent explicitly called coding_finish with success=false "
                        "and no workspace changes. Preserving the reported blocker instead of "
                        "relabeling the run as a successful no-op."
                    ),
                    "start_commit": str(kwargs.get("start_head") or ""),
                    "end_commit": str(kwargs.get("end_head") or ""),
                },
            )
        return original(**kwargs)

    agent._no_change_audit = audit_with_blocker_semantics
    agent._coding_contract_blocked_finish_audit_installed = True


def _install_debug_event_view(debug_report: Any) -> None:
    if bool(getattr(debug_report, "_coding_contract_event_view_installed", False)):
        return
    original = debug_report._event_view
    safe_policy_keys = (
        "previous_signature",
        "policy_signature",
        "previous_action_kind",
        "action_kind",
        "source_backend",
        "text_tool_mode",
        "allowed_tools",
        "causal_evidence_targets",
        "acceptance_evidence_targets",
        "hypothesis_causal_evidence_linked",
        "removed_empty_assistant_messages",
        "converted_tool_calls",
        "converted_tool_results",
        "clipped_tool_results",
        "history_messages_before_compaction",
        "history_messages_after_compaction",
    )

    def event_view_with_policy(event: Dict[str, Any]) -> Dict[str, Any]:
        out = original(event)
        for key in safe_policy_keys:
            if key in event and event.get(key) not in (None, ""):
                out[key] = debug_report._sanitize(event.get(key), key=key)
        return out

    debug_report._event_view = event_view_with_policy
    debug_report._coding_contract_event_view_installed = True


def _read_matches_target(requested: str, target: str) -> bool:
    if not requested or not target:
        return False
    if "/" in target:
        return requested == target
    return PurePosixPath(requested).name == target


def install(agent: Any, evidence_policy: Any, debug_report: Any) -> None:
    """Install Coding-Workspace-only contract hardening after the existing overlays."""
    if bool(getattr(agent, "_coding_contract_hardening_installed", False)):
        return

    base = evidence_policy._base_policy(agent.forced_action)
    base._HYPOTHESIS_FIELD_RE = _hypothesis_pattern(
        tuple(str(label) for label in base._HYPOTHESIS_FIELDS)
    )

    original_apply = evidence_policy.apply_provenance_gate

    def apply_with_recovery(
        forced_action: Any,
        task: Mapping[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = original_apply(forced_action, task, state)
        return refine_provenance_state(evidence_policy, forced_action, task, result)

    evidence_policy.apply_provenance_gate = apply_with_recovery
    evidence_policy._provenance_prompt_context = provenance_prompt_context

    original_evaluate = evidence_policy.ExecutionForcedActionFacade.evaluate_tool_call

    def evaluate_with_target_lock(
        self: Any,
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, Dict[str, Any]]:
        allowed, details = original_evaluate(
            self,
            task,
            name=name,
            args=args,
            is_validation_command=is_validation_command,
        )
        if not allowed or str(name or "") != "coding_read_file_lines":
            return allowed, details

        state = self.active_state(task)
        targets = [
            str(item)
            for item in (state.get("hypothesis_unverified_targets") or [])
            if str(item).strip()
        ]
        if not targets:
            return allowed, details

        requested = _normalized_path(args.get("path"))
        if requested and any(_read_matches_target(requested, target) for target in targets):
            return True, {}
        return False, {
            "ok": False,
            "error": "forced_action_tool_rejected",
            "message": (
                "Forced-action mode permits one corrective evidence read only for the target "
                "cited by the structured hypothesis. "
                f"Requested {requested or '(unsafe or missing path)'}; permitted targets: "
                f"{', '.join(sorted(targets))}."
            ),
            "required_action": state.get("required_action"),
            "action_kind": state.get("action_kind"),
            "allowed_tools": sorted(state.get("allowed_tools") or []),
            "hypothesis_unverified_targets": sorted(targets),
            "state_key": state.get("state_key"),
        }

    evidence_policy.ExecutionForcedActionFacade.evaluate_tool_call = evaluate_with_target_lock

    _install_invalid_tool_diagnostics(agent)
    _install_blocked_finish_audit(agent)
    _install_debug_event_view(debug_report)
    agent._coding_contract_hardening_installed = True
