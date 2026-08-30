from __future__ import annotations

import asyncio
import difflib
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA = "nexus_coding_mission_acceptance_epoch.v1"
KEY = "coding_mission_acceptance_epoch"
REFUTATION_SCHEMA = "nexus_coding_hypothesis_refutation.v1"
REFUTATION_KEY = "coding_hypothesis_refutation"
REFUTATION_TOOL = "coding_refute_hypothesis"
_MAX_UNTRACKED = 200
_MAX_UNTRACKED_BYTES = 100_000
_MAX_REVIEW_DIFF_CHARS = 40_000


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _consume_semantic_review_identity(
    value: Any,
) -> tuple[Dict[str, Any], Mapping[str, Any]]:
    result = dict(value) if isinstance(value, Mapping) else {}
    identity = _mapping(
        result.pop("_semantic_acceptance_review_identity", {})
    )
    return result, identity


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mutate_task(cw: Any, task_id: str, apply: Any) -> Dict[str, Any]:
    mutate = getattr(cw, "mutate_task", None)
    if callable(mutate):
        return mutate(task_id, apply)
    task = cw.load_task(task_id)
    apply(task)
    return cw.save_task(task)


def _repo_path(cw: Any, task: Mapping[str, Any]) -> Path:
    resolver = getattr(cw, "_repo_path", None)
    if callable(resolver):
        return Path(resolver(dict(task))).resolve()
    return Path(str(task.get("repo_path") or "")).resolve()


def _run_process(cw: Any, argv: list[str], *, cwd: Path) -> Dict[str, Any]:
    runner = getattr(cw, "_run_process", None)
    if not callable(runner):
        return {"ok": False, "stdout": "", "stderr": "workspace git runner unavailable"}
    return runner(argv, cwd=cwd, timeout_sec=30.0)


def _stdout(result: Any) -> str:
    return str(result.get("stdout") or "").strip() if isinstance(result, Mapping) else ""


def _head(cw: Any, task_id: str, task: Optional[Mapping[str, Any]] = None) -> str:
    try:
        result = cw.git_head(task_id)
        value = str(result.get("commit") or "").strip() if isinstance(result, Mapping) else ""
        if value:
            return value
    except Exception:
        pass
    task = task or {}
    return str(task.get("last_commit") or task.get("last_checkpoint_commit") or "").strip()


def _resolve_acceptance_base(cw: Any, task_id: str, task: Mapping[str, Any]) -> str:
    repo = _repo_path(cw, task)
    base_branch = str(task.get("base_branch") or "main").strip() or "main"
    base_diff = getattr(cw, "_git_base_branch_diff", None)
    if callable(base_diff):
        try:
            result = base_diff(repo, base_branch=base_branch)
        except Exception:
            result = {}
        if isinstance(result, Mapping):
            candidate = str(result.get("merge_base") or result.get("compare_ref") or "").strip()
            if candidate:
                resolved = _run_process(cw, ["git", "rev-parse", candidate], cwd=repo)
                sha = _stdout(resolved)
                if bool(resolved.get("ok")) and sha:
                    return sha
    for candidate in (
        str(task.get("mission_base_head") or "").strip(),
        str(task.get("initial_head") or "").strip(),
        str(task.get("agent_initial_head") or "").strip(),
    ):
        if candidate:
            return candidate
    return _head(cw, task_id, task)


def ensure_epoch(
    cw: Any,
    task_id: str,
    task: Optional[Dict[str, Any]] = None,
    *,
    observed_last_edit_at: float = 0.0,
) -> Dict[str, Any]:
    task = dict(task or cw.load_task(task_id))
    current = _mapping(task.get(KEY))
    valid_current = (
        str(current.get("schema") or "") == SCHEMA
        and bool(str(current.get("base_head") or "").strip())
    )
    if valid_current and (not observed_last_edit_at or _float(current.get("last_mutation_at"))):
        return dict(current)

    if valid_current:
        proposed = dict(current)
    else:
        base_head = _resolve_acceptance_base(cw, task_id, task)
        if not base_head:
            return {
                "schema": SCHEMA,
                "status": "error",
                "base_head": "",
                "error": "unable to establish mission acceptance base",
            }
        now = time.time()
        proposed = {
            "schema": SCHEMA,
            "status": "pending",
            "base_head": base_head,
            "base_branch": str(task.get("base_branch") or "main").strip() or "main",
            "created_at": now,
            "updated_at": now,
            "last_mutation_at": 0.0,
            "last_mutation_run_id": "",
            "accepted_at": 0.0,
            "accepted_head": "",
            "accepted_run_id": "",
            "accepted_fingerprint": "",
            "accepted_diff_sha256": "",
            "acceptance_publication_generation": 0,
            "finalized_at": 0.0,
            "finalized_head": "",
        }
    if observed_last_edit_at and not _float(proposed.get("last_mutation_at")):
        proposed["last_mutation_at"] = float(observed_last_edit_at)
        proposed["updated_at"] = time.time()

    def apply(latest: Dict[str, Any]) -> None:
        existing = _mapping(latest.get(KEY))
        if (
            str(existing.get("schema") or "") == SCHEMA
            and str(existing.get("base_head") or "").strip()
        ):
            if observed_last_edit_at and not _float(existing.get("last_mutation_at")):
                updated = dict(existing)
                updated["last_mutation_at"] = float(observed_last_edit_at)
                updated["updated_at"] = time.time()
                latest[KEY] = updated
            return
        latest[KEY] = dict(proposed)

    stored = _mutate_task(cw, task_id, apply)
    return dict(_mapping(stored.get(KEY)) or proposed)


def _safe_untracked_diff(cw: Any, *, repo: Path) -> tuple[str, str]:
    result = _run_process(cw, ["git", "ls-files", "--others", "--exclude-standard"], cwd=repo)
    if not bool(result.get("ok")):
        return "", str(result.get("stderr") or result.get("error") or "git ls-files failed")
    paths = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    if len(paths) > _MAX_UNTRACKED:
        return "", f"mission delta has {len(paths)} untracked files; limit is {_MAX_UNTRACKED}"
    pieces: list[str] = []
    for raw in paths:
        candidate = repo.joinpath(raw).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            return "", f"untracked path escapes workspace: {raw}"
        if not candidate.is_file():
            continue
        try:
            data = candidate.read_bytes()
        except Exception as exc:
            return "", f"unable to read untracked file {raw}: {type(exc).__name__}: {exc}"
        if len(data) > _MAX_UNTRACKED_BYTES:
            pieces.append(
                f"diff --git a/{raw} b/{raw}\nnew file mode 100644\n"
                f"Binary or oversized untracked file ({len(data)} bytes)"
            )
            continue
        if b"\x00" in data:
            pieces.append(
                f"diff --git a/{raw} b/{raw}\nnew file mode 100644\nBinary untracked file"
            )
            continue
        text = data.decode("utf-8", errors="replace")
        rendered = "".join(
            difflib.unified_diff(
                [],
                text.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{raw}",
            )
        ).strip()
        if rendered:
            pieces.append(rendered)
    return "\n\n".join(pieces).strip(), ""


def mission_delta_state(
    cw: Any,
    task_id: str,
    task: Optional[Dict[str, Any]] = None,
    *,
    observed_last_edit_at: float = 0.0,
) -> Dict[str, Any]:
    task = dict(task or cw.load_task(task_id))
    epoch = ensure_epoch(
        cw,
        task_id,
        task,
        observed_last_edit_at=observed_last_edit_at,
    )
    base_head = str(epoch.get("base_head") or "").strip()
    if not base_head or str(epoch.get("status") or "") == "error":
        return {
            "ok": False,
            "has_delta": False,
            "base_head": base_head,
            "current_head": _head(cw, task_id, task),
            "diff_text": "",
            "diff_sha256": "",
            "error": str(epoch.get("error") or "mission acceptance base unavailable"),
            "epoch": epoch,
        }
    repo = _repo_path(cw, task)
    tracked = _run_process(
        cw,
        ["git", "diff", "--no-ext-diff", "--binary", base_head, "--", "."],
        cwd=repo,
    )
    if not bool(tracked.get("ok")):
        return {
            "ok": False,
            "has_delta": False,
            "base_head": base_head,
            "current_head": _head(cw, task_id, task),
            "diff_text": "",
            "diff_sha256": "",
            "error": str(tracked.get("stderr") or tracked.get("error") or "git diff failed"),
            "epoch": epoch,
        }
    untracked, untracked_error = _safe_untracked_diff(cw, repo=repo)
    if untracked_error:
        return {
            "ok": False,
            "has_delta": False,
            "base_head": base_head,
            "current_head": _head(cw, task_id, task),
            "diff_text": "",
            "diff_sha256": "",
            "error": untracked_error,
            "epoch": epoch,
        }
    tracked_text = str(tracked.get("stdout") or "").strip()
    pieces = [part for part in (tracked_text, untracked) if part]
    raw = "\n\n".join(pieces).strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""
    tracked_digest = (
        hashlib.sha256(tracked_text.encode("utf-8")).hexdigest() if tracked_text else ""
    )
    return {
        "ok": True,
        "has_delta": bool(raw),
        "base_head": base_head,
        "current_head": _head(cw, task_id, task),
        "diff_text": raw,
        "diff_sha256": digest,
        "tracked_diff_sha256": tracked_digest,
        "diff_chars": len(raw),
        "error": "",
        "epoch": epoch,
    }


def mission_review_diff(cw: Any, agent: Any, task_id: str, task: Dict[str, Any]) -> str:
    state = mission_delta_state(cw, task_id, task)
    if not state.get("ok") or not state.get("has_delta"):
        return ""
    raw = str(state.get("diff_text") or "")
    clipped = agent._clip_text(raw, _MAX_REVIEW_DIFF_CHARS)
    return (
        f"Mission acceptance base: {state.get('base_head') or ''}\n"
        f"Mission delta SHA256: {state.get('diff_sha256') or ''}\n"
        f"Mission delta characters: {len(raw)}\n\n{clipped}"
    ).strip()


def _acceptance_fingerprint(
    terminal_hardening: Any,
    task: Mapping[str, Any],
    *,
    review_diff: str,
) -> str:
    fn = getattr(terminal_hardening, "semantic_acceptance_fingerprint", None)
    if not callable(fn) or not review_diff:
        return ""
    try:
        return str(fn(task, diff_text=review_diff) or "")
    except Exception:
        return ""


def _latest_accepted_review(task: Mapping[str, Any]) -> Mapping[str, Any]:
    cycle = _int(task.get("agent_cycle"))
    for raw in reversed(list(task.get("agent_events") or [])):
        event = _mapping(raw)
        if str(event.get("type") or "") != "semantic_acceptance_review":
            continue
        if _int(event.get("cycle")) != cycle:
            continue
        return event if event.get("accepted") is True else {}
    return {}


def _epoch_accepted_for_current(
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> bool:
    epoch = _mapping(task.get(KEY))
    if str(epoch.get("schema") or "") != SCHEMA:
        return False
    review_diff = mission_review_diff(cw, agent, task_id, dict(task))
    if not review_diff:
        return False
    fingerprint = _acceptance_fingerprint(
        terminal_hardening,
        task,
        review_diff=review_diff,
    )
    return bool(fingerprint) and fingerprint == str(epoch.get("accepted_fingerprint") or "")


def _record_semantic_acceptance(
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    *,
    reviewed_fingerprint: str = "",
    reviewed_cycle: Optional[int] = None,
    return_publication: bool = False,
) -> Any:
    bound_fingerprint = str(reviewed_fingerprint or "").strip()
    if not bound_fingerprint or reviewed_cycle is None:
        return {} if return_publication else False
    bound_cycle = _int(reviewed_cycle)
    initial = cw.load_task(task_id)
    if _int(initial.get("agent_cycle")) != bound_cycle:
        return {} if return_publication else False
    reviewed_fingerprint = bound_fingerprint
    reviewed_cycle = bound_cycle
    max_attempts = 2

    task = initial
    for attempt in range(max_attempts):
        if attempt:
            task = cw.load_task(task_id)
            if _int(task.get("agent_cycle")) != reviewed_cycle:
                return False

        state = mission_delta_state(cw, task_id, task)
        if not state.get("ok") or not state.get("has_delta"):
            return False
        review_diff = mission_review_diff(cw, agent, task_id, task)
        fingerprint = _acceptance_fingerprint(
            terminal_hardening,
            task,
            review_diff=review_diff,
        )
        if not fingerprint:
            return False
        # Publication and retry are both bound to the exact workspace accepted
        # by this review. A stale reviewer can never retry over newer state.
        if reviewed_fingerprint and fingerprint != reviewed_fingerprint:
            return False

        now = time.time()
        base_head = str(state.get("base_head") or "")
        current_head = str(state.get("current_head") or "")
        run_id = str(task.get("agent_run_id") or "")
        diff_sha = str(state.get("diff_sha256") or "")
        observed_epoch = _mapping(task.get(KEY))
        observed_accepted_fingerprint = str(
            observed_epoch.get("accepted_fingerprint") or ""
        ).strip()
        observed_last_mutation_at = _float(observed_epoch.get("last_mutation_at"))
        observed_last_mutation_run_id = str(
            observed_epoch.get("last_mutation_run_id") or ""
        )
        observed_publication_generation = _int(
            observed_epoch.get("acceptance_publication_generation")
        )
        published = False
        published_generation = 0

        def apply(latest: Dict[str, Any]) -> None:
            nonlocal published, published_generation
            if _int(latest.get("agent_cycle")) != reviewed_cycle:
                return
            current = dict(_mapping(latest.get(KEY)))
            if str(current.get("base_head") or "") != base_head:
                return
            current_accepted_fingerprint = str(
                current.get("accepted_fingerprint") or ""
            ).strip()
            current_publication_generation = _int(
                current.get("acceptance_publication_generation")
            )
            publication_generation_unchanged = (
                current_publication_generation == observed_publication_generation
            )
            acceptance_unchanged = (
                publication_generation_unchanged
                and current_accepted_fingerprint == observed_accepted_fingerprint
            )
            cleanup_cleared_observed_stale = (
                attempt > 0
                and publication_generation_unchanged
                and bool(observed_accepted_fingerprint)
                and not current_accepted_fingerprint
                and str(current.get("status") or "") == "pending"
                and not _float(current.get("accepted_at"))
                and not str(current.get("accepted_head") or "").strip()
                and not str(current.get("accepted_run_id") or "").strip()
                and not str(current.get("accepted_diff_sha256") or "").strip()
                and _float(current.get("last_mutation_at"))
                == observed_last_mutation_at
                and str(current.get("last_mutation_run_id") or "")
                == observed_last_mutation_run_id
            )
            if not acceptance_unchanged and not cleanup_cleared_observed_stale:
                return
            current.update(
                {
                    "status": "semantic_accepted",
                    "accepted_at": now,
                    "accepted_head": current_head,
                    "accepted_run_id": run_id,
                    "accepted_fingerprint": fingerprint,
                    "accepted_diff_sha256": diff_sha,
                    "acceptance_publication_generation": observed_publication_generation + 1,
                    "updated_at": now,
                }
            )
            latest[KEY] = current
            published_generation = observed_publication_generation + 1
            published = True

        _mutate_task(cw, task_id, apply)
        if published:
            if return_publication:
                return {
                    "fingerprint": fingerprint,
                    "publication_generation": published_generation,
                }
            return True
        # A CAS loss is retried at most once, after reloading and revalidating
        # that this review still describes the live workspace. This lets a
        # current recorder recover when a stale recorder briefly wins first.

    return {} if return_publication else False


def _record_mutation(cw: Any, task_id: str) -> None:
    task = cw.load_task(task_id)
    epoch = dict(ensure_epoch(cw, task_id, task))
    now = time.time()
    run_id = str(task.get("agent_run_id") or "")
    base_head = str(epoch.get("base_head") or "")

    def apply(latest: Dict[str, Any]) -> None:
        current = dict(_mapping(latest.get(KEY)))
        if str(current.get("base_head") or "") != base_head:
            return
        current.update(
            {
                "status": "pending",
                "last_mutation_at": now,
                "last_mutation_run_id": run_id,
                "accepted_at": 0.0,
                "accepted_head": "",
                "accepted_run_id": "",
                "accepted_fingerprint": "",
                "accepted_diff_sha256": "",
                "updated_at": now,
            }
        )
        latest[KEY] = current
        refutation = _mapping(latest.get(REFUTATION_KEY))
        if (
            str(refutation.get("schema") or "") == REFUTATION_SCHEMA
            and str(refutation.get("status") or "") == "active"
        ):
            consumed = dict(refutation)
            consumed.update(
                {
                    "status": "consumed",
                    "consumed_at": now,
                    "consumed_run_id": run_id,
                }
            )
            latest[REFUTATION_KEY] = consumed

    _mutate_task(cw, task_id, apply)


def _evidence_count_since(forced_action: Any, task: Mapping[str, Any], since: float) -> int:
    count = 0
    targeted = set(
        getattr(
            forced_action,
            "_TARGETED_EVIDENCE_TOOLS",
            {"coding_search_text", "coding_read_file_lines"},
        )
    )
    succeeded = getattr(forced_action, "_targeted_evidence_result_succeeded", None)
    for raw in task.get("agent_events") or []:
        event = _mapping(raw)
        if _float(event.get("ts")) < since:
            continue
        if str(event.get("type") or "") != "tool_finished":
            continue
        name = str(event.get("name") or "")
        if name not in targeted:
            continue
        result = _mapping(event.get("result"))
        if callable(succeeded):
            try:
                if not bool(succeeded(name, result)):
                    continue
            except Exception:
                continue
        elif result.get("ok") is False or str(result.get("error") or ""):
            continue
        count += 1
    return count


def _refutation_overlay_state(
    forced_action: Any,
    task: Mapping[str, Any],
    original_state: Mapping[str, Any],
) -> Dict[str, Any]:
    refutation = _mapping(task.get(REFUTATION_KEY))
    if (
        str(refutation.get("schema") or "") != REFUTATION_SCHEMA
        or str(refutation.get("status") or "") != "active"
    ):
        return dict(original_state)

    raw_forced = _mapping(task.get("agent_forced_action"))
    if not original_state and str(raw_forced.get("status") or "") != "active":
        return {}
    state = dict(original_state or raw_forced)
    since = _float(refutation.get("refuted_at"))
    evidence_count = _evidence_count_since(forced_action, task, since)
    limit = _int(getattr(forced_action, "_MAX_TARGETED_EVIDENCE_ACTIONS", 2)) or 2
    plan = _mapping(task.get("project_plan"))
    plan_revision = _int(plan.get("revision"))
    refutation_revision = _int(refutation.get("plan_revision"))
    hypothesis_ready = False
    fields: Dict[str, str] = {}
    structured = getattr(forced_action, "_structured_hypothesis", None)
    if plan_revision > refutation_revision and callable(structured):
        try:
            hypothesis_ready, fields = structured(
                task,
                {"activation_plan_revision": refutation_revision},
            )
        except Exception:
            hypothesis_ready, fields = False, {}

    state["requires_hypothesis"] = True
    state["targeted_evidence_count"] = evidence_count
    state["targeted_evidence_limit"] = limit
    state["hypothesis_ready"] = bool(hypothesis_ready)
    state["hypothesis_fields"] = sorted(fields)
    state["hypothesis_plan_revision"] = plan_revision if hypothesis_ready else None
    state["refutation_count"] = _int(refutation.get("count"))
    state["refutation_plan_revision"] = refutation_revision

    if evidence_count > 0 and hypothesis_ready:
        state["action_kind"] = "edit"
        state["required_action"] = (
            "The prior remediation hypothesis was explicitly refuted and replaced with fresh evidence. "
            "Make the smallest evidence-backed edit, or finish with a concrete blocker."
        )
        state["allowed_tools"] = sorted(
            {
                "coding_write_file",
                "coding_replace_text",
                "coding_apply_patch",
                "coding_finish",
            }
        )
        return state

    allowed = {"coding_update_plan", "coding_finish"}
    if evidence_count < limit:
        allowed.update({"coding_search_text", "coding_read_file_lines"})
    state["action_kind"] = "evidence"
    state["required_action"] = (
        "The current remediation hypothesis was explicitly refuted. Gather at most two targeted repository evidence actions, "
        "then replace it with a fresh four-field hypothesis using coding_update_plan before editing."
    )
    state["allowed_tools"] = sorted(allowed)
    return state


def _record_refutation(
    agent: Any,
    cw: Any,
    task_id: str,
    *,
    reason: str,
    contradicting_evidence: str,
    forced_state: Mapping[str, Any],
) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    plan = _mapping(task.get("project_plan"))
    previous = _mapping(task.get(REFUTATION_KEY))
    previous_count = (
        _int(previous.get("count"))
        if str(previous.get("status") or "") == "active"
        else 0
    )
    now = time.time()
    refutation = {
        "schema": REFUTATION_SCHEMA,
        "status": "active",
        "count": previous_count + 1,
        "plan_revision": _int(plan.get("revision")),
        "refuted_at": now,
        "run_id": str(task.get("agent_run_id") or ""),
        "cycle": _int(task.get("agent_cycle")),
        "state_key": str(forced_state.get("state_key") or ""),
        "reason": str(reason or "").strip()[:4000],
        "contradicting_evidence": str(contradicting_evidence or "").strip()[:6000],
    }

    def apply(latest: Dict[str, Any]) -> None:
        latest[REFUTATION_KEY] = dict(refutation)

    _mutate_task(cw, task_id, apply)
    append = getattr(agent, "_append_event", None)
    if callable(append):
        append(
            task_id,
            {
                "type": "hypothesis_refuted",
                "cycle": refutation["cycle"],
                "plan_revision": refutation["plan_revision"],
                "refutation_count": refutation["count"],
                "reason": refutation["reason"],
                "summary": (
                    "The agent explicitly refuted the current remediation hypothesis. "
                    "Editing is suspended until a bounded fresh-evidence pass supports a replacement hypothesis."
                ),
            },
        )
    return refutation


def _snapshot_ready(snapshot: Mapping[str, Any]) -> bool:
    validation = _mapping(snapshot.get("validation"))
    review = _mapping(snapshot.get("diff_review"))
    return (
        bool(validation.get("validation_after_latest_edit"))
        and validation.get("last_validation_ok") is True
        and bool(review.get("diff_reviewed_after_latest_edit"))
    )


def _reconcile_snapshot(
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    task: Dict[str, Any],
    snapshot: Mapping[str, Any],
) -> Dict[str, Any]:
    output = dict(snapshot)
    changes = dict(_mapping(output.get("changes")))
    observed_last_edit = _float(changes.get("last_edit_at"))
    state = mission_delta_state(
        cw,
        task_id,
        task,
        observed_last_edit_at=observed_last_edit,
    )
    epoch = _mapping(state.get("epoch"))
    mission_last_mutation = max(
        observed_last_edit,
        _float(epoch.get("last_mutation_at")),
    )
    if mission_last_mutation:
        changes["last_edit_at"] = mission_last_mutation
        output["changes"] = changes
        validation = dict(_mapping(output.get("validation")))
        validation_at = _float(validation.get("last_validation_at"))
        validation["validation_after_latest_edit"] = bool(
            validation_at and validation_at >= mission_last_mutation
        )
        output["validation"] = validation
        review = dict(_mapping(output.get("diff_review")))
        review_at = _float(review.get("last_diff_review_at"))
        review["diff_reviewed_after_latest_edit"] = bool(
            review_at and review_at >= mission_last_mutation
        )
        output["diff_review"] = review

    accepted_current = False
    if state.get("ok") and state.get("has_delta"):
        accepted_current = _epoch_accepted_for_current(
            terminal_hardening,
            cw,
            agent,
            task_id,
            task,
        )
    output["mission_acceptance"] = {
        "schema": SCHEMA,
        "base_head": str(state.get("base_head") or ""),
        "current_head": str(state.get("current_head") or ""),
        "has_delta": bool(state.get("has_delta")),
        "diff_sha256": str(state.get("diff_sha256") or ""),
        "diff_chars": _int(state.get("diff_chars")),
        "semantic_accepted": accepted_current,
        "accepted_head": str(epoch.get("accepted_head") or ""),
        "accepted_run_id": str(epoch.get("accepted_run_id") or ""),
        "status": str(epoch.get("status") or ""),
        "error": str(state.get("error") or ""),
    }

    if not state.get("ok") or not state.get("has_delta"):
        return output
    progress = dict(_mapping(output.get("progress")))
    validation = _mapping(output.get("validation"))
    review = _mapping(output.get("diff_review"))
    if not bool(validation.get("validation_after_latest_edit")):
        progress["current_phase"] = "editing"
        progress["next_recommended_action"] = "validate mission changes"
    elif validation.get("last_validation_ok") is not True:
        progress["current_phase"] = "editing"
        progress["next_recommended_action"] = "resolve failed mission validation"
    elif not bool(review.get("diff_reviewed_after_latest_edit")):
        progress["current_phase"] = "reviewing"
        progress["next_recommended_action"] = "review mission diff"
    elif accepted_current:
        progress["current_phase"] = "finalizing"
        progress["next_recommended_action"] = "finalize accepted mission changes"
    else:
        progress["current_phase"] = "finalizing"
        progress["next_recommended_action"] = "finish the mission for semantic acceptance"
    output["progress"] = progress
    return output


def _refutation_tool_spec(agent: Any) -> Any:
    return agent.ToolSpec(
        function=agent.ToolFunction(
            name=REFUTATION_TOOL,
            description=(
                "Suspend forced edit mode only when verified repository evidence contradicts the current remediation hypothesis. "
                "This opens one bounded evidence pass and does not revise the project plan by itself."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why verified repository evidence contradicts the current remediation hypothesis.",
                    },
                    "contradicting_evidence": {
                        "type": "string",
                        "description": "Concise repository evidence supporting the refutation.",
                    },
                },
                "required": ["reason"],
            },
        )
    )


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    forced_action: Any,
    terminal_hardening: Any,
) -> None:
    """Make acceptance follow the durable workspace mission rather than one runner attempt."""
    if bool(getattr(guarded, "_mission_acceptance_epoch_installed", False)):
        return

    original_run_delta_diff = guarded._run_delta_diff
    original_run_tool = guarded._run_tool_with_semantic_acceptance
    if not callable(
        getattr(guarded, "_run_tool_with_semantic_acceptance_before_mission_acceptance_epoch", None)
    ):
        guarded._run_tool_with_semantic_acceptance_before_mission_acceptance_epoch = original_run_tool
    original_start_agent_run = agent.start_agent_run
    original_requires_edits = agent._mission_requires_workspace_edits
    original_finalize = agent.finalize_successful_run
    if not callable(
        getattr(agent, "_finalize_successful_run_before_mission_acceptance_epoch", None)
    ):
        agent._finalize_successful_run_before_mission_acceptance_epoch = original_finalize
    original_snapshot = cw.coding_state_snapshot
    original_active_state = forced_action.active_state
    original_tool_specs = agent._tool_specs
    original_allowed_tool_names = getattr(forced_action, "allowed_tool_names", None)
    original_filter_tool_specs = getattr(forced_action, "filter_tool_specs", None)
    original_evaluate_tool_call = getattr(forced_action, "evaluate_tool_call", None)
    original_prompt_context = getattr(forced_action, "prompt_context", None)

    def tool_specs_with_refutation() -> list[Any]:
        specs = list(original_tool_specs())
        if not any(
            str(getattr(getattr(item, "function", None), "name", "")) == REFUTATION_TOOL
            for item in specs
        ):
            specs.append(_refutation_tool_spec(agent))
        return specs

    agent._tool_specs = tool_specs_with_refutation

    def mission_delta_diff(task_id: str, task: Dict[str, Any]) -> str:
        try:
            rendered = mission_review_diff(cw, agent, task_id, task)
        except Exception:
            rendered = ""
        if rendered:
            return rendered
        return original_run_delta_diff(task_id, task)

    guarded._run_delta_diff = mission_delta_diff

    def active_state_with_refutation(task: Mapping[str, Any]) -> Dict[str, Any]:
        original = original_active_state(task)
        return _refutation_overlay_state(forced_action, task, original)

    forced_action.active_state = active_state_with_refutation

    if callable(original_allowed_tool_names):
        def allowed_tool_names_with_refutation(task: Mapping[str, Any]) -> set[str]:
            names = set(original_allowed_tool_names(task))
            state = forced_action.active_state(task)
            if state and str(state.get("action_kind") or "") == "edit":
                names.add(REFUTATION_TOOL)
            return names

        forced_action.allowed_tool_names = allowed_tool_names_with_refutation

    if callable(original_filter_tool_specs):
        def filter_tool_specs_with_refutation(specs: Sequence[Any], task: Mapping[str, Any]) -> list[Any]:
            state = forced_action.active_state(task)
            if not state:
                return [
                    item
                    for item in specs
                    if str(getattr(getattr(item, "function", None), "name", "")) != REFUTATION_TOOL
                ]
            allowed = (
                forced_action.allowed_tool_names(task)
                if callable(getattr(forced_action, "allowed_tool_names", None))
                else set(state.get("allowed_tools") or [])
            )
            return [
                item
                for item in specs
                if str(getattr(getattr(item, "function", None), "name", "")) in allowed
            ]

        forced_action.filter_tool_specs = filter_tool_specs_with_refutation

    if callable(original_evaluate_tool_call):
        def evaluate_tool_call_with_refutation(
            task: Mapping[str, Any],
            *,
            name: str,
            args: Mapping[str, Any],
            is_validation_command: Any,
        ) -> tuple[bool, Dict[str, Any]]:
            if str(name or "") == REFUTATION_TOOL:
                state = forced_action.active_state(task)
                if state and str(state.get("action_kind") or "") == "edit":
                    return True, {}
            return original_evaluate_tool_call(
                task,
                name=name,
                args=args,
                is_validation_command=is_validation_command,
            )

        forced_action.evaluate_tool_call = evaluate_tool_call_with_refutation

    if callable(original_prompt_context):
        def prompt_context_with_refutation(task: Mapping[str, Any]) -> str:
            rendered = str(original_prompt_context(task) or "")
            state = forced_action.active_state(task)
            if state and str(state.get("action_kind") or "") == "edit":
                rendered = (
                    f"{rendered} If verified repository evidence contradicts the current remediation hypothesis, "
                    "coding_refute_hypothesis is the only authorized escape back to evidence gathering; generic plan churn remains disabled."
                ).strip()
            return rendered

        forced_action.prompt_context = prompt_context_with_refutation

    def run_tool_with_mission_acceptance(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        try:
            before_task = cw.load_task(task_id)
        except Exception:
            delegated, _ = _consume_semantic_review_identity(
                original_run_tool(
                    task_id,
                    name,
                    args,
                    git_token_value=git_token_value,
                )
            )
            if (
                name == "coding_finish"
                and delegated.get("ok") is True
                and delegated.get("success") is True
            ):
                return {
                    "ok": False,
                    "success": False,
                    "error": "mission_acceptance_state_unavailable",
                    "required_action": "Retry coding_finish after mission acceptance state is available.",
                    "summary": (
                        "Mission acceptance state could not be loaded before coding_finish. "
                        "The delegated success was sanitized and blocked from finalization; retry coding_finish."
                    ),
                }
            return delegated

        forced_state = forced_action.active_state(before_task)
        if name == REFUTATION_TOOL:
            if str(forced_state.get("action_kind") or "") != "edit":
                return {
                    "ok": False,
                    "error": "hypothesis_refutation_not_available",
                    "summary": "coding_refute_hypothesis is available only while forced edit mode is active.",
                }
            reason = str(args.get("reason") or "").strip()
            if len(reason) < 8:
                return {
                    "ok": False,
                    "error": "hypothesis_refutation_reason_required",
                    "summary": "Provide a concrete reason grounded in verified repository evidence.",
                }
            previous = _mapping(before_task.get(REFUTATION_KEY))
            count = (
                _int(previous.get("count"))
                if str(previous.get("status") or "") == "active"
                else 0
            )
            if count >= 2:
                return {
                    "ok": False,
                    "error": "hypothesis_refutation_limit",
                    "required_action": "Make the evidence-backed edit or finish with a concrete blocker.",
                    "summary": "The bounded hypothesis-refutation escape has already been used twice without a consuming mutation.",
                }
            refutation = _record_refutation(
                agent,
                cw,
                task_id,
                reason=reason,
                contradicting_evidence=str(args.get("contradicting_evidence") or ""),
                forced_state=forced_state,
            )
            return {
                "ok": True,
                "refuted": True,
                "refutation_count": refutation["count"],
                "required_action": (
                    "Gather at most two targeted repository evidence actions, then record a fresh four-field remediation hypothesis with coding_update_plan."
                ),
                "summary": "The current remediation hypothesis was explicitly refuted; forced edit mode is suspended for one bounded evidence pass.",
            }

        result, review_identity = _consume_semantic_review_identity(
            original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )
        )
        reviewed_fingerprint = str(
            review_identity.get("fingerprint") or ""
        ).strip()
        raw_reviewed_cycle = review_identity.get("cycle")
        reviewed_cycle = (
            _int(raw_reviewed_cycle) if raw_reviewed_cycle is not None else None
        )

        mutation = bool(result.get("workspace_modified")) or (
            name in {"coding_write_file", "coding_replace_text", "coding_apply_patch"}
            and result.get("ok") is True
        )
        if mutation:
            try:
                _record_mutation(cw, task_id)
            except Exception:
                pass

        if name == "coding_finish" and result.get("ok") is True and result.get("success") is True:
            try:
                state = mission_delta_state(cw, task_id)
            except Exception:
                return {
                    "ok": False,
                    "success": False,
                    "error": "mission_acceptance_state_unavailable",
                    "required_action": "Retry coding_finish after mission acceptance state is available.",
                    "summary": (
                        "Mission acceptance state could not be loaded after coding_finish. "
                        "The delegated success was sanitized and blocked; retry coding_finish."
                    ),
                }
            if not state.get("ok"):
                return {
                    "ok": False,
                    "success": False,
                    "error": "mission_acceptance_state_unavailable",
                    "required_action": "Retry coding_finish after mission acceptance state is available.",
                    "summary": (
                        "Mission delta state is unavailable after coding_finish. "
                        "The delegated success was blocked instead of being treated as terminal."
                    ),
                }
            if state.get("has_delta"):
                try:
                    latest = cw.load_task(task_id)
                    accepted = _epoch_accepted_for_current(
                        terminal_hardening,
                        cw,
                        agent,
                        task_id,
                        latest,
                    )
                    if not accepted and reviewed_fingerprint:
                        _record_semantic_acceptance(
                            terminal_hardening,
                            cw,
                            agent,
                            task_id,
                            reviewed_fingerprint=reviewed_fingerprint,
                            reviewed_cycle=reviewed_cycle,
                        )
                        latest = cw.load_task(task_id)
                        accepted = _epoch_accepted_for_current(
                            terminal_hardening,
                            cw,
                            agent,
                            task_id,
                            latest,
                        )
                except Exception:
                    return {
                        "ok": False,
                        "success": False,
                        "error": "mission_acceptance_state_unavailable",
                        "required_action": "Retry coding_finish after mission acceptance state is available.",
                        "summary": (
                            "Mission acceptance could not be reloaded or verified after coding_finish. "
                            "The delegated success was blocked; retry coding_finish."
                        ),
                    }
                if not accepted:
                    return {
                        "ok": False,
                        "success": False,
                        "error": "mission_semantic_acceptance_missing",
                        "summary": (
                            "The workspace contains an unaccepted mission delta, including inherited checkpoint changes. "
                            "A successful independent semantic acceptance review of the full mission delta is required before finishing."
                        ),
                    }
        return result

    guarded._run_tool_with_semantic_acceptance = run_tool_with_mission_acceptance
    agent._run_tool = run_tool_with_mission_acceptance

    async def start_agent_run_with_epoch(task_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            task = await asyncio.to_thread(cw.load_task, task_id)
            await asyncio.to_thread(ensure_epoch, cw, task_id, task)
        except Exception:
            pass
        return await original_start_agent_run(task_id, *args, **kwargs)

    agent.start_agent_run = start_agent_run_with_epoch

    def mission_requires_workspace_edits(task: Dict[str, Any]) -> bool:
        task_id = str(task.get("id") or "").strip()
        if task_id:
            try:
                state = mission_delta_state(cw, task_id, task)
                if state.get("ok") and state.get("has_delta"):
                    return False
            except Exception:
                pass
        return original_requires_edits(task)

    agent._mission_requires_workspace_edits = mission_requires_workspace_edits

    def finalize_successful_mission(
        task_id: str,
        *,
        mission: Optional[Dict[str, Any]] = None,
        git_token_value: Optional[str] = None,
        finish_summary: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        try:
            task = cw.load_task(task_id)
            state = mission_delta_state(cw, task_id, task)
            snapshot = cw.coding_state_snapshot(task_id)
            accepted = _epoch_accepted_for_current(
                terminal_hardening,
                cw,
                agent,
                task_id,
                task,
            )
        except Exception:
            return {
                "ok": False,
                "error": "mission_acceptance_state_unavailable",
                "required_action": "Retry coding_finish after mission acceptance state is available.",
                "summary": (
                    "Mission acceptance state could not be established during finalization. "
                    "Finalization was blocked instead of falling through to the unguarded finalizer."
                ),
            }
        if not state.get("ok"):
            return {
                "ok": False,
                "error": "mission_acceptance_state_unavailable",
                "required_action": "Retry coding_finish after mission acceptance state is available.",
                "summary": (
                    "Mission delta state is unavailable during finalization. "
                    "Finalization was blocked instead of falling through to the unguarded finalizer."
                ),
            }
        if state.get("has_delta") and not accepted:
            return {
                "ok": False,
                "error": "mission_semantic_acceptance_missing",
                "required_action": "Retry coding_finish to obtain semantic acceptance for the current mission delta.",
                "summary": (
                    "The workspace contains a mission delta without current semantic acceptance. "
                    "Finalization was blocked before commit or push."
                ),
            }
        if state.get("has_delta") and accepted and _snapshot_ready(snapshot):
            contract = cw.normalize_coding_mission(task, mission)
            patched = dict(contract)
            completion = dict(_mapping(contract.get("completion_policy")))
            completion["require_file_changes"] = False
            patched["completion_policy"] = completion
            result = original_finalize(
                task_id,
                mission=patched,
                git_token_value=git_token_value,
                finish_summary=finish_summary,
                run_id=run_id,
            )
            if result.get("ok") is True:
                now = time.time()
                final_head = str(state.get("current_head") or "")

                def apply(latest: Dict[str, Any]) -> None:
                    current = dict(_mapping(latest.get(KEY)))
                    if current.get("accepted_fingerprint"):
                        current.update(
                            {
                                "status": "finalized",
                                "finalized_at": now,
                                "finalized_head": final_head,
                                "updated_at": now,
                            }
                        )
                        latest[KEY] = current

                try:
                    _mutate_task(cw, task_id, apply)
                except Exception:
                    pass
            return result
        return original_finalize(
            task_id,
            mission=mission,
            git_token_value=git_token_value,
            finish_summary=finish_summary,
            run_id=run_id,
        )

    agent.finalize_successful_run = finalize_successful_mission

    def snapshot_with_mission_acceptance(task_id: str) -> Dict[str, Any]:
        snapshot = original_snapshot(task_id)
        try:
            task = cw.load_task(task_id)
            return _reconcile_snapshot(
                terminal_hardening,
                cw,
                agent,
                task_id,
                task,
                snapshot,
            )
        except Exception:
            return snapshot

    cw.coding_state_snapshot = snapshot_with_mission_acceptance
    guarded._mission_acceptance_epoch_installed = True
