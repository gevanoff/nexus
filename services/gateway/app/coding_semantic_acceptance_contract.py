from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA = "nexus_coding_mission_acceptance_contract.v1"
KEY = "coding_mission_acceptance_contract"
_MAX_GROUNDING_CHARS = 30_000
_MAX_WIDE_DIFF_CHARS = 18_000
_MAX_HELPERS = 8
_HELPER_CONTEXT_LINES = 28
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IGNORED_CALLS = {
    "bool",
    "dict",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "print",
    "range",
    "set",
    "str",
    "super",
    "tuple",
}
_SEMANTIC_EVENT_FIELDS = (
    "accepted",
    "reason",
    "causal_alignment",
    "existing_mechanism_checked",
    "acceptance_criteria_checked",
    "review_error",
    "fingerprint",
)
_LOGGER = logging.getLogger(__name__)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_criteria(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [line.strip(" -*\t") for line in value.splitlines()]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = [str(item or "").strip() for item in value]
    else:
        values = []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = " ".join(item.split()).strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized[:2000])
    return out[:40]


def _contract_payload(task: Mapping[str, Any]) -> Dict[str, Any]:
    mission = _mapping(task.get("mission"))
    criteria = _normalize_criteria(task.get("mission_acceptance_criteria"))
    if not criteria:
        criteria = _normalize_criteria(mission.get("acceptance_criteria"))
    return {
        "original_request": str(task.get("prompt") or mission.get("goal") or "").strip(),
        "acceptance_criteria": criteria,
    }


def _virtual_contract(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Return deterministic contract identity even before durable materialization."""
    payload = _contract_payload(task)
    return {
        "schema": SCHEMA,
        **payload,
        "fingerprint": _stable_hash(payload),
        "immutable": True,
    }


def _is_frozen_contract(value: Any) -> bool:
    contract = _mapping(value)
    if str(contract.get("schema") or "") != SCHEMA:
        return False
    fingerprint = str(contract.get("fingerprint") or "").strip()
    if not fingerprint:
        return False
    payload = {
        "original_request": str(contract.get("original_request") or "").strip(),
        "acceptance_criteria": _normalize_criteria(contract.get("acceptance_criteria")),
    }
    return fingerprint == _stable_hash(payload)


def _materialize_contract(task: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        **_virtual_contract(task),
        "created_at": time.time(),
    }


def ensure_contract(cw: Any, task_id: str, task: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    source = dict(task or cw.load_task(task_id))
    existing = _mapping(source.get(KEY))
    if _is_frozen_contract(existing):
        return dict(existing)

    def apply(latest: Dict[str, Any]) -> None:
        current = _mapping(latest.get(KEY))
        if _is_frozen_contract(current):
            return
        latest[KEY] = _materialize_contract(latest)

    mutate = getattr(cw, "mutate_task", None)
    if callable(mutate):
        stored = mutate(task_id, apply)
    else:
        latest = cw.load_task(task_id)
        apply(latest)
        stored = cw.save_task(latest)
    frozen = _mapping(stored.get(KEY))
    return dict(frozen if _is_frozen_contract(frozen) else _materialize_contract(stored))


def set_acceptance_criteria(cw: Any, task_id: str, criteria: Any) -> Dict[str, Any]:
    normalized = _normalize_criteria(criteria)
    if not normalized:
        raise ValueError("at least one non-empty acceptance criterion is required")

    def apply(latest: Dict[str, Any]) -> None:
        current = _mapping(latest.get(KEY))
        if _is_frozen_contract(current):
            existing_criteria = _normalize_criteria(current.get("acceptance_criteria"))
            if existing_criteria == normalized:
                return
            raise ValueError("mission acceptance contract is already immutable for this workspace")
        latest["mission_acceptance_criteria"] = list(normalized)
        latest[KEY] = _materialize_contract(latest)

    mutate = getattr(cw, "mutate_task", None)
    if callable(mutate):
        stored = mutate(task_id, apply)
    else:
        latest = cw.load_task(task_id)
        apply(latest)
        stored = cw.save_task(latest)
    frozen = _mapping(stored.get(KEY))
    if not _is_frozen_contract(frozen):
        raise RuntimeError("mission acceptance contract was not persisted")
    return dict(frozen)


def render_contract(contract: Mapping[str, Any]) -> str:
    request = str(contract.get("original_request") or "").strip() or "(none)"
    criteria = _normalize_criteria(contract.get("acceptance_criteria"))
    lines = [
        f"Contract schema: {contract.get('schema') or SCHEMA}",
        f"Contract fingerprint: {contract.get('fingerprint') or ''}",
        "Original user request (immutable):",
        request,
        "Acceptance criteria (immutable; agent-authored plan text cannot replace these):",
    ]
    if criteria:
        lines.extend(f"- {item}" for item in criteria)
    else:
        lines.append("- No additional operator-supplied criteria; evaluate the original request literally.")
    return "\n".join(lines).strip()


def _added_or_removed_calls(diff_text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(diff_text or "").splitlines():
        if not raw or raw.startswith(("+++", "---")) or raw[0] not in "+-":
            continue
        for match in _CALL_RE.finditer(raw[1:]):
            name = match.group(1)
            if name in _IGNORED_CALLS or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= _MAX_HELPERS:
                return out
    return out


def _definition_context(epoch: Any, cw: Any, *, repo: Path, name: str) -> str:
    escaped = re.escape(name)
    pattern = (
        rf"(^|[[:space:]])(async[[:space:]]+)?def[[:space:]]+{escaped}[[:space:]]*[(]|"
        rf"(^|[[:space:]])function[[:space:]]+{escaped}[[:space:]]*[(]|"
        rf"(^|[[:space:]])class[[:space:]]+{escaped}([[:space:](]|$)"
    )
    result = epoch._run_process(
        cw,
        ["git", "grep", "-n", "-E", pattern, "--", "."],
        cwd=repo,
    )
    if not bool(result.get("ok")):
        return ""
    pieces: list[str] = []
    for hit in str(result.get("stdout") or "").splitlines()[:2]:
        parts = hit.split(":", 2)
        if len(parts) < 3:
            continue
        raw_path, raw_line, _text = parts
        try:
            line_no = int(raw_line)
        except ValueError:
            continue
        candidate = repo.joinpath(raw_path).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        start = max(0, line_no - 1 - _HELPER_CONTEXT_LINES)
        end = min(len(lines), line_no + _HELPER_CONTEXT_LINES)
        numbered = "\n".join(
            f"{index + 1:>6}: {lines[index]}" for index in range(start, end)
        )
        pieces.append(f"Definition context for {name} at {raw_path}:{line_no}:\n{numbered}")
    return "\n\n".join(pieces).strip()


def repository_grounding(epoch: Any, cw: Any, agent: Any, task_id: str, task: Mapping[str, Any]) -> str:
    state = epoch.mission_delta_state(cw, task_id, dict(task))
    if not state.get("ok") or not state.get("has_delta"):
        return ""
    repo = epoch._repo_path(cw, task)
    base_head = str(state.get("base_head") or "").strip()
    pieces: list[str] = []
    if base_head:
        wide = epoch._run_process(
            cw,
            ["git", "diff", "--no-ext-diff", "--unified=80", base_head, "--", "."],
            cwd=repo,
        )
        if bool(wide.get("ok")):
            text = str(wide.get("stdout") or "").strip()
            if text:
                pieces.append(
                    "Wide repository context around the changed lines:\n"
                    + agent._clip_text(text, _MAX_WIDE_DIFF_CHARS)
                )

    diff_text = str(state.get("diff_text") or "")
    for name in _added_or_removed_calls(diff_text):
        context = _definition_context(epoch, cw, repo=repo, name=name)
        if context:
            pieces.append(context)

    raw = "\n\n".join(pieces).strip()
    return agent._clip_text(raw, _MAX_GROUNDING_CHARS) if raw else ""


def _contract_fingerprint_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    frozen = _mapping(task.get(KEY))
    contract = frozen if _is_frozen_contract(frozen) else _virtual_contract(task)
    return {
        "schema": SCHEMA,
        "fingerprint": str(contract.get("fingerprint") or ""),
        "original_request": str(contract.get("original_request") or ""),
        "acceptance_criteria": _normalize_criteria(contract.get("acceptance_criteria")),
    }


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    epoch: Any,
    terminal_hardening: Any,
    semantic_acceptance: Any,
    debug_report: Any,
) -> None:
    """Bind final semantic acceptance to immutable mission intent and repository ground truth."""
    if bool(getattr(guarded, "_semantic_acceptance_contract_installed", False)):
        return
    if not bool(getattr(agent, "_coding_live_refutation_execution_installed", False)):
        raise RuntimeError("semantic acceptance contract must be installed after acceptance convergence")

    original_review = guarded._semantic_acceptance_review
    original_fingerprint = terminal_hardening.semantic_acceptance_fingerprint

    def semantic_acceptance_fingerprint(task: Mapping[str, Any], *, diff_text: str) -> str:
        base = str(original_fingerprint(task, diff_text=diff_text) or "")
        if not base:
            return ""
        return _stable_hash(
            {
                "base_fingerprint": base,
                "acceptance_contract": _contract_fingerprint_state(task),
            }
        )

    terminal_hardening.semantic_acceptance_fingerprint = semantic_acceptance_fingerprint

    def retryable_review_failure(
        task_id: str,
        task: Mapping[str, Any],
        diff_text: str,
        exc: Exception,
    ) -> Dict[str, Any]:
        fingerprint = ""
        try:
            latest = cw.load_task(task_id)
            fingerprint = terminal_hardening.semantic_acceptance_fingerprint(
                latest,
                diff_text=diff_text,
            )
        except Exception:
            pass
        return {
            "accepted": False,
            "reason": (
                "semantic acceptance contract/repository grounding failed: "
                f"{type(exc).__name__}: {str(exc)[:1200]}"
            ),
            "causal_alignment": False,
            "existing_mechanism_checked": False,
            "acceptance_criteria_checked": False,
            "review_error": True,
            "fingerprint": fingerprint,
        }

    async def grounded_review(
        task_id: str,
        task: Dict[str, Any],
        *,
        diff_text: str,
    ) -> Dict[str, Any]:
        try:
            expected_contract = _contract_fingerprint_state(task)
            latest = cw.load_task(task_id)
            frozen = ensure_contract(cw, task_id, latest)
            latest = cw.load_task(task_id)
            actual_contract = _contract_fingerprint_state(latest)
            fingerprint = terminal_hardening.semantic_acceptance_fingerprint(
                latest,
                diff_text=diff_text,
            )
            if expected_contract.get("fingerprint") != actual_contract.get("fingerprint"):
                return {
                    "accepted": False,
                    "reason": (
                        "mission acceptance contract changed while semantic review was starting; "
                        "retry coding_finish against the newly frozen operator intent"
                    ),
                    "causal_alignment": False,
                    "existing_mechanism_checked": False,
                    "acceptance_criteria_checked": False,
                    "review_error": True,
                    "fingerprint": fingerprint,
                }

            evidence = repository_grounding(epoch, cw, agent, task_id, latest)
            token = semantic_acceptance.set_review_grounding(
                acceptance_contract=render_contract(frozen),
                repository_evidence=evidence,
            )
            try:
                review = dict(await original_review(task_id, latest, diff_text=diff_text))
            finally:
                semantic_acceptance.reset_review_grounding(token)
            review["review_error"] = bool(review.get("review_error") or review.get("parse_error"))
            review["fingerprint"] = fingerprint
            return review
        except Exception as exc:
            return retryable_review_failure(task_id, task, diff_text, exc)

    guarded._semantic_acceptance_review = grounded_review

    original_prior_rejection = getattr(terminal_hardening, "_prior_rejection", None)
    if callable(original_prior_rejection):
        def prior_rejection_if_live(task: Mapping[str, Any], fingerprint: str) -> Mapping[str, Any]:
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                return original_prior_rejection(task, fingerprint)
            try:
                latest = cw.load_task(task_id)
                live_diff = str(guarded._run_delta_diff(task_id, dict(latest)) or "")
                live_fingerprint = terminal_hardening.semantic_acceptance_fingerprint(
                    latest,
                    diff_text=live_diff,
                ) if live_diff else ""
            except Exception:
                # Non-production/synthetic callers may invoke the owning module
                # directly without a task in this installed workspace store.
                return original_prior_rejection(task, fingerprint)
            if not live_fingerprint or live_fingerprint != fingerprint:
                return {}
            return original_prior_rejection(latest, fingerprint)

        terminal_hardening._prior_rejection = prior_rejection_if_live
        terminal_hardening._prior_rejection_before_semantic_contract = original_prior_rejection

    try:
        from app import coding_acceptance_convergence_hardening as convergence
    except Exception:
        convergence = None
    if convergence is not None and not bool(
        getattr(convergence, "_semantic_contract_rejection_guard_installed", False)
    ):
        original_guard_key = getattr(convergence, "_semantic_rejection_guard_key", None)
        original_guard_blocks = getattr(convergence, "_semantic_rejection_guard_blocks", None)
        if callable(original_guard_key):
            def rejection_guard_key_with_contract(
                cw_obj: Any,
                mission_epoch_obj: Any,
                task_id: str,
                task: Mapping[str, Any],
            ) -> str:
                base = str(original_guard_key(cw_obj, mission_epoch_obj, task_id, task) or "")
                if not base:
                    return ""
                return _stable_hash(
                    {
                        "base_causal_key": base,
                        "acceptance_contract": _contract_fingerprint_state(task),
                    }
                )

            convergence._semantic_rejection_guard_key = rejection_guard_key_with_contract
            convergence._semantic_rejection_guard_key_before_semantic_contract = original_guard_key

        if callable(original_guard_blocks):
            def rejection_guard_blocks_live_contract(
                cw_obj: Any,
                mission_epoch_obj: Any,
                task_id: str,
                task: Mapping[str, Any],
            ) -> bool:
                if cw_obj is not cw:
                    return bool(original_guard_blocks(cw_obj, mission_epoch_obj, task_id, task))
                try:
                    before = cw_obj.load_task(task_id)
                except Exception:
                    before = dict(task)
                blocked = bool(original_guard_blocks(cw_obj, mission_epoch_obj, task_id, before))
                if not blocked:
                    return False
                try:
                    after = cw_obj.load_task(task_id)
                except Exception:
                    return blocked
                if (
                    _contract_fingerprint_state(before).get("fingerprint")
                    != _contract_fingerprint_state(after).get("fingerprint")
                ):
                    return False
                return bool(original_guard_blocks(cw_obj, mission_epoch_obj, task_id, after))

            convergence._semantic_rejection_guard_blocks = rejection_guard_blocks_live_contract
            convergence._semantic_rejection_guard_blocks_before_semantic_contract = original_guard_blocks
        convergence._semantic_contract_rejection_guard_installed = True

    original_record_rejection = getattr(terminal_hardening, "_record_rejection", None)
    if callable(original_record_rejection):
        def record_rejection_with_reviewed_fingerprint(
            agent_obj: Any,
            task_id: str,
            task: Mapping[str, Any],
            *,
            fingerprint: str,
            result: Mapping[str, Any],
        ) -> None:
            review = _mapping(result.get("semantic_review"))
            reviewed_fingerprint = str(review.get("fingerprint") or "").strip()
            original_record_rejection(
                agent_obj,
                task_id,
                task,
                fingerprint=reviewed_fingerprint or fingerprint,
                result=result,
            )

        terminal_hardening._record_rejection = record_rejection_with_reviewed_fingerprint
        terminal_hardening._record_rejection_before_semantic_contract = original_record_rejection

    original_record_semantic_acceptance = getattr(epoch, "_record_semantic_acceptance", None)
    latest_accepted_review = getattr(epoch, "_latest_accepted_review", None)
    mission_review_diff = getattr(epoch, "mission_review_diff", None)
    epoch_key = str(getattr(epoch, "KEY", "coding_mission_acceptance_epoch"))
    epoch_schema = str(getattr(epoch, "SCHEMA", "nexus_coding_mission_acceptance_epoch.v1"))

    def current_review_fingerprint(
        terminal_obj: Any,
        cw_obj: Any,
        agent_obj: Any,
        task_id: str,
        task: Mapping[str, Any],
    ) -> str:
        if not callable(mission_review_diff):
            return ""
        rendered = str(mission_review_diff(cw_obj, agent_obj, task_id, dict(task)) or "")
        fn = getattr(terminal_obj, "semantic_acceptance_fingerprint", None)
        if not rendered or not callable(fn):
            return ""
        try:
            return str(fn(task, diff_text=rendered) or "")
        except Exception:
            return ""

    def clear_stale_epoch_acceptance(
        cw_obj: Any,
        task_id: str,
        *,
        stale_fingerprint: str,
        stale_publication_generation: int,
    ) -> None:
        target = str(stale_fingerprint or "").strip()
        target_generation = int(stale_publication_generation or 0)
        if not target or target_generation <= 0:
            return

        def apply(latest: Dict[str, Any]) -> None:
            current = dict(_mapping(latest.get(epoch_key)))
            if str(current.get("schema") or "") != epoch_schema:
                return
            if str(current.get("accepted_fingerprint") or "").strip() != target:
                return
            if int(current.get("acceptance_publication_generation") or 0) != target_generation:
                return
            current.update(
                {
                    "status": "pending",
                    "accepted_at": 0.0,
                    "accepted_head": "",
                    "accepted_run_id": "",
                    "accepted_fingerprint": "",
                    "accepted_diff_sha256": "",
                    "updated_at": time.time(),
                }
            )
            latest[epoch_key] = current

        mutate = getattr(cw_obj, "mutate_task", None)
        if callable(mutate):
            mutate(task_id, apply)
        else:
            latest = cw_obj.load_task(task_id)
            apply(latest)
            cw_obj.save_task(latest)

    if callable(original_record_semantic_acceptance):
        def record_semantic_acceptance_if_review_current(
            terminal_obj: Any,
            cw_obj: Any,
            agent_obj: Any,
            task_id: str,
            *,
            reviewed_fingerprint: str = "",
            reviewed_cycle: Optional[int] = None,
            return_publication: bool = False,
        ) -> None:
            if terminal_obj is not terminal_hardening or cw_obj is not cw or agent_obj is not agent:
                return original_record_semantic_acceptance(
                    terminal_obj,
                    cw_obj,
                    agent_obj,
                    task_id,
                    reviewed_fingerprint=reviewed_fingerprint,
                    reviewed_cycle=reviewed_cycle,
                    return_publication=return_publication,
                )

            before = cw_obj.load_task(task_id)
            reviewed_fingerprint = str(reviewed_fingerprint or "").strip()
            if not reviewed_fingerprint or reviewed_cycle is None:
                message = (
                    "Refusing to record semantic acceptance because no complete "
                    "invocation-local review identity was supplied."
                )
                _LOGGER.warning("%s task_id=%s", message, task_id)
                try:
                    agent_obj._append_event(
                        task_id,
                        {
                            "type": "semantic_acceptance_record_blocked",
                            "cycle": int(before.get("agent_cycle") or 0),
                            "accepted": False,
                            "review_error": True,
                            "fingerprint": "",
                            "reason": message,
                            "summary": (
                                "Semantic acceptance publication was blocked instead of silently "
                                "recording or dropping an unbound accepted review."
                            ),
                        },
                    )
                except Exception:
                    pass
                return

            effective_reviewed_cycle = int(reviewed_cycle)
            if int(before.get("agent_cycle") or 0) != effective_reviewed_cycle:
                return

            current_fingerprint = current_review_fingerprint(
                terminal_obj,
                cw_obj,
                agent_obj,
                task_id,
                before,
            )
            if reviewed_fingerprint != current_fingerprint:
                return

            publication = original_record_semantic_acceptance(
                terminal_obj,
                cw_obj,
                agent_obj,
                task_id,
                reviewed_fingerprint=reviewed_fingerprint,
                reviewed_cycle=effective_reviewed_cycle,
                return_publication=True,
            )
            publication_info = _mapping(publication)
            published_fingerprint = str(
                publication_info.get("fingerprint") or ""
            ).strip()
            published_generation = int(
                publication_info.get("publication_generation") or 0
            )
            if not published_fingerprint or published_generation <= 0:
                return

            after = cw_obj.load_task(task_id)
            after_fingerprint = current_review_fingerprint(
                terminal_obj,
                cw_obj,
                agent_obj,
                task_id,
                after,
            )
            if reviewed_fingerprint != after_fingerprint:
                clear_stale_epoch_acceptance(
                    cw_obj,
                    task_id,
                    stale_fingerprint=published_fingerprint,
                    stale_publication_generation=published_generation,
                )

        epoch._record_semantic_acceptance = record_semantic_acceptance_if_review_current
        epoch._record_semantic_acceptance_before_semantic_contract = original_record_semantic_acceptance

    original_event_view = debug_report._event_view

    def event_view(event: Dict[str, Any]) -> Dict[str, Any]:
        output = original_event_view(event)
        if str(event.get("type") or "") in {
            "semantic_acceptance_review",
            "semantic_acceptance_state",
            "semantic_acceptance_repeat_blocked",
            "semantic_acceptance_record_blocked",
        }:
            for key in _SEMANTIC_EVENT_FIELDS:
                if key not in event:
                    continue
                value = event.get(key)
                output[key] = (
                    debug_report.redact_text(value, limit=2400)
                    if isinstance(value, str)
                    else value
                )
        return output

    debug_report._event_view = event_view

    original_collect = debug_report.collect_debug_snapshot

    def collect_debug_snapshot(task_id: str, *, active_runner: Optional[bool] = None) -> Dict[str, Any]:
        snapshot = original_collect(task_id, active_runner=active_runner)
        task = cw.load_task(task_id)
        frozen = _mapping(task.get(KEY))
        if _is_frozen_contract(frozen):
            snapshot["mission_acceptance_contract"] = debug_report._sanitize(dict(frozen))
        return snapshot

    debug_report.collect_debug_snapshot = collect_debug_snapshot
    guarded._semantic_acceptance_contract_installed = True
