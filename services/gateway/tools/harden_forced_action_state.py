from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


forced_path = ROOT / "services/gateway/app/coding_forced_action.py"
replace_once(
    forced_path,
    '''def allowed_tool_names(task: Mapping[str, Any]) -> set[str]:
    return set(_ALLOWED_TOOLS) if active_state(task) else set()


def evaluate_tool_call(
''',
    '''def allowed_tool_names(task: Mapping[str, Any]) -> set[str]:
    return set(_ALLOWED_TOOLS) if active_state(task) else set()


def rejection_counter_for_state(
    previous_state_key: str,
    previous_count: int,
    task: Mapping[str, Any],
) -> tuple[str, int]:
    current_key = str(active_state(task).get("state_key") or "")
    if current_key != str(previous_state_key or ""):
        return current_key, 0
    return current_key, max(0, int(previous_count or 0))


def evaluate_tool_call(
''',
    "add state-scoped rejection counter",
)

semantic_path = ROOT / "services/gateway/app/coding_semantic_memory.py"
replace_once(
    semantic_path,
    '''    if str(lease.get("status") or "") != "granted":
        return False
    if resilience.durable_state_key(task) != str(lease.get("state_key") or ""):
''',
    '''    if str(lease.get("status") or "") != "granted":
        return False
    if str(lease.get("kind") or "") == "continuation":
        def retire_legacy_continuation(latest: Dict[str, Any]) -> None:
            current = (
                latest.get("agent_stagnation_recovery_lease")
                if isinstance(latest.get("agent_stagnation_recovery_lease"), dict)
                else {}
            )
            if (
                str(current.get("id") or "") != str(lease.get("id") or "")
                or str(current.get("status") or "") != "granted"
                or str(current.get("kind") or "") != "continuation"
            ):
                return
            current = dict(current)
            current.update({
                "status": "superseded",
                "remaining_transitions": 0,
                "superseded_at": time.time(),
                "superseded_reason": "continuation recovery no longer resets unchanged state",
            })
            latest["agent_stagnation_recovery_lease"] = current

        cw.mutate_task(task_id, retire_legacy_continuation)
        return False
    if resilience.durable_state_key(task) != str(lease.get("state_key") or ""):
''',
    "retire legacy granted continuation lease",
)

agent_path = ROOT / "services/gateway/app/coding_agent.py"
replace_once(
    agent_path,
    '''        semantic_reroutes = 0
        forced_action_rejections = 0
        semantic_failed_backends: set[str] = set()
''',
    '''        semantic_reroutes = 0
        forced_action_rejections = 0
        forced_action_rejection_state_key = ""
        semantic_failed_backends: set[str] = set()
''',
    "initialize forced action rejection state key",
)
replace_once(
    agent_path,
    '''            latest_policy_task = await asyncio.to_thread(cw.load_task, task_id)
            tools = _tool_specs_for_task(latest_policy_task)
            context_chars = _messages_char_count(messages)
''',
    '''            latest_policy_task = await asyncio.to_thread(cw.load_task, task_id)
            tools = _tool_specs_for_task(latest_policy_task)
            forced_action_rejection_state_key, forced_action_rejections = (
                forced_action.rejection_counter_for_state(
                    forced_action_rejection_state_key,
                    forced_action_rejections,
                    latest_policy_task,
                )
            )
            context_chars = _messages_char_count(messages)
''',
    "scope rejection count to forced state key",
)
replace_once(
    agent_path,
    '''        require_commit_on_success = bool(completion.get("require_commit_on_success", True)) and (expects_workspace_edits or actual_delta)
''',
    '''        require_commit_on_success = bool(completion.get("require_commit_on_success", True)) or actual_delta
''',
    "require commit when review produces delta",
)

forced_test = ROOT / "services/gateway/tests/test_coding_forced_action.py"
replace_once(
    forced_test,
    '''def test_forced_action_allows_only_edits_validation_diff_or_finish():
''',
    '''def test_rejection_counter_resets_when_forced_state_changes_or_expires():
    task = _task()
    first_key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=first_key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )
    key, count = forced.rejection_counter_for_state("", 1, task)
    assert key == first_key
    assert count == 0
    key, count = forced.rejection_counter_for_state(key, 1, task)
    assert count == 1

    task["agent_progress_state"]["observation"]["workspace_fingerprint"] = "edited"
    key, count = forced.rejection_counter_for_state(key, 1, task)
    assert key == ""
    assert count == 0


def test_forced_action_allows_only_edits_validation_diff_or_finish():
''',
    "add rejection counter regression",
)

stagnation_test = ROOT / "services/gateway/tests/test_coding_stagnation_resilience.py"
replace_once(
    stagnation_test,
    '''def test_guidance_interventions_are_scoped_to_run_but_recovery_credit_is_not():
''',
    '''def test_granted_legacy_continuation_lease_is_retired_without_reset(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_recovery_lease"] = {
        "schema": "nexus_coding_recovery_lease.v1",
        "id": f"{key}:legacy-continuation",
        "state_key": key,
        "kind": "continuation",
        "run_id": "run-2",
        "granted_cycle": 1,
        "remaining_transitions": 1,
        "status": "granted",
    }
    task["agent_cycle"] = 2
    _install_workspace(monkeypatch, task)

    assert memory._consume_recovery_lease(task["id"], task) is False
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_stagnation_recovery_lease"]["status"] == "superseded"
    assert task["agent_stagnation_recovery_lease"]["remaining_transitions"] == 0


def test_guidance_interventions_are_scoped_to_run_but_recovery_credit_is_not():
''',
    "add legacy continuation lease regression",
)

mission_test = ROOT / "services/gateway/tests/test_coding_mission_finalization.py"
replace_once(
    mission_test,
    '''def _finalizer_mocks(monkeypatch, *, changed=True, commit_ok=True):
''',
    '''def _finalizer_mocks(monkeypatch, *, changed=True, base_delta=True, commit_ok=True):
''',
    "separate working tree and base delta test states",
)
replace_once(
    mission_test,
    '''    monkeypatch.setattr(cw, "git_diff", lambda *_a, **_k: {"ok": True, "changes": {"counts": {"total": 1}}})
''',
    '''    monkeypatch.setattr(cw, "git_diff", lambda *_a, **_k: {"ok": True, "changes": {"counts": {"total": 1 if base_delta else 0}}})
''',
    "model no-change base diff",
)
replace_once(
    mission_test,
    '''    stored = _finalizer_mocks(monkeypatch, changed=False)
''',
    '''    stored = _finalizer_mocks(monkeypatch, changed=False, base_delta=False)
''',
    "make review no-change test genuine",
)
replace_once(
    mission_test,
    '''def test_coding_finalization_push_on_success(monkeypatch):
''',
    '''def test_review_finalization_commits_when_review_produces_delta(monkeypatch):
    stored = _finalizer_mocks(monkeypatch, changed=True, base_delta=True)
    stored["prompt"] = "Review this workspace for concrete findings and missing tests."
    stored["mission"] = {
        "completion_policy": {
            "require_file_changes": False,
            "require_commit_on_success": False,
            "require_validation_after_edit": True,
            "require_diff_review_after_edit": True,
        }
    }
    commits = []
    monkeypatch.setattr(
        cw,
        "commit_task",
        lambda *_a, **_k: commits.append(True) or {"ok": True, "last_commit": "review-fix"},
    )

    result = ca.finalize_successful_run("task-1", finish_summary="Found and fixed a defect.", run_id="run-1")

    assert result["ok"] is True
    assert commits == [True]
    assert result["final_commit"] == "review-fix"


def test_coding_finalization_push_on_success(monkeypatch):
''',
    "add review delta commit regression",
)

print("forced-action state hardening applied")
