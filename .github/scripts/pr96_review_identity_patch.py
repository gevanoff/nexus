from pathlib import Path


def replace_once(path_s: str, old: str, new: str) -> None:
    path = Path(path_s)
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path_s}: expected one marker, got {count}")
    path.write_text(text.replace(old, new, 1))


replace_once(
    "services/gateway/app/coding_agent_guarded.py",
    '''    if bool(review.get("accepted")):
        return result
''',
    '''    if bool(review.get("accepted")):
        accepted_result = dict(result)
        accepted_result["_semantic_acceptance_review_identity"] = {
            "fingerprint": str(review.get("fingerprint") or "").strip(),
            "cycle": int(task.get("agent_cycle") or 0),
        }
        return accepted_result
''',
)

replace_once(
    "services/gateway/app/coding_mission_acceptance_epoch.py",
    '''def _record_semantic_acceptance(
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    *,
    return_publication: bool = False,
) -> Any:
    initial = cw.load_task(task_id)
    review = _latest_accepted_review(initial)
    if not review:
        return False
    reviewed_fingerprint = str(review.get("fingerprint") or "").strip()
    reviewed_cycle = _int(initial.get("agent_cycle"))
    max_attempts = 2 if reviewed_fingerprint else 1
''',
    '''def _record_semantic_acceptance(
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    *,
    reviewed_fingerprint: str = "",
    reviewed_cycle: Optional[int] = None,
    return_publication: bool = False,
) -> Any:
    initial = cw.load_task(task_id)
    explicit_review_identity = reviewed_cycle is not None or bool(
        str(reviewed_fingerprint or "").strip()
    )
    if explicit_review_identity:
        bound_fingerprint = str(reviewed_fingerprint or "").strip()
        bound_cycle = _int(reviewed_cycle)
    else:
        review = _latest_accepted_review(initial)
        if not review:
            return False
        bound_fingerprint = str(review.get("fingerprint") or "").strip()
        bound_cycle = _int(review.get("cycle") or initial.get("agent_cycle"))
    if not bound_fingerprint:
        return False
    if _int(initial.get("agent_cycle")) != bound_cycle:
        return False
    reviewed_fingerprint = bound_fingerprint
    reviewed_cycle = bound_cycle
    max_attempts = 2
''',
)

replace_once(
    "services/gateway/app/coding_mission_acceptance_epoch.py",
    '''        result = original_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )

        mutation = bool(result.get("workspace_modified")) or (
''',
    '''        result = dict(
            original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )
        )
        review_identity = _mapping(
            result.pop("_semantic_acceptance_review_identity", {})
        )
        reviewed_fingerprint = str(
            review_identity.get("fingerprint") or ""
        ).strip()
        raw_reviewed_cycle = review_identity.get("cycle")
        reviewed_cycle = (
            _int(raw_reviewed_cycle) if raw_reviewed_cycle is not None else None
        )

        mutation = bool(result.get("workspace_modified")) or (
''',
)

replace_once(
    "services/gateway/app/coding_mission_acceptance_epoch.py",
    '''            if state.get("ok") and state.get("has_delta"):
                try:
                    _record_semantic_acceptance(
                        terminal_hardening,
                        cw,
                        agent,
                        task_id,
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
                    accepted = False
''',
    '''            if state.get("ok") and state.get("has_delta"):
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
                    accepted = False
''',
)

replace_once(
    "services/gateway/app/coding_semantic_acceptance_contract.py",
    '''        def record_semantic_acceptance_if_review_current(
            terminal_obj: Any,
            cw_obj: Any,
            agent_obj: Any,
            task_id: str,
        ) -> None:
''',
    '''        def record_semantic_acceptance_if_review_current(
            terminal_obj: Any,
            cw_obj: Any,
            agent_obj: Any,
            task_id: str,
            *,
            reviewed_fingerprint: str = "",
            reviewed_cycle: Optional[int] = None,
            return_publication: bool = False,
        ) -> None:
''',
)

replace_once(
    "services/gateway/app/coding_semantic_acceptance_contract.py",
    '''            before = cw_obj.load_task(task_id)
            review = _mapping(latest_accepted_review(before))
            reviewed_fingerprint = str(review.get("fingerprint") or "").strip()
            if not reviewed_fingerprint:
                if not _is_frozen_contract(before.get(KEY)):
                    return original_record_semantic_acceptance(
                        terminal_obj,
                        cw_obj,
                        agent_obj,
                        task_id,
                    )
                message = (
                    "Refusing to record semantic acceptance for a frozen mission contract because "
                    "the latest accepted review has no acceptance fingerprint."
                )
''',
    '''            before = cw_obj.load_task(task_id)
            explicit_review_identity = reviewed_cycle is not None or bool(
                str(reviewed_fingerprint or "").strip()
            )
            if explicit_review_identity:
                reviewed_fingerprint = str(reviewed_fingerprint or "").strip()
                effective_reviewed_cycle = (
                    int(reviewed_cycle)
                    if reviewed_cycle is not None
                    else int(before.get("agent_cycle") or 0)
                )
            else:
                review = _mapping(latest_accepted_review(before))
                reviewed_fingerprint = str(review.get("fingerprint") or "").strip()
                effective_reviewed_cycle = int(
                    review.get("cycle") or before.get("agent_cycle") or 0
                )
            if not reviewed_fingerprint:
                message = (
                    "Refusing to record semantic acceptance because the latest accepted review "
                    "has no acceptance fingerprint."
                )
''',
)

replace_once(
    "services/gateway/app/coding_semantic_acceptance_contract.py",
    '''                publication = original_record_semantic_acceptance(
                    terminal_obj,
                    cw_obj,
                    agent_obj,
                    task_id,
                    return_publication=True,
                )
            except TypeError as exc:
                # Synthetic/legacy recorders may not expose the structured-return
                # keyword. Run them for compatibility, but without an exact
                # publication identity stale cleanup must fail closed.
                if "return_publication" not in str(exc):
                    raise
                original_record_semantic_acceptance(
                    terminal_obj,
                    cw_obj,
                    agent_obj,
                    task_id,
                )
                publication = {}
''',
    '''                publication = original_record_semantic_acceptance(
                    terminal_obj,
                    cw_obj,
                    agent_obj,
                    task_id,
                    reviewed_fingerprint=reviewed_fingerprint,
                    reviewed_cycle=effective_reviewed_cycle,
                    return_publication=True,
                )
            except TypeError as exc:
                # Synthetic/legacy recorders may not expose invocation identity.
                # Production owner code does; compatibility recorders still need
                # an exact structured publication identity for cleanup.
                if not any(
                    token in str(exc)
                    for token in (
                        "reviewed_fingerprint",
                        "reviewed_cycle",
                        "return_publication",
                    )
                ):
                    raise
                try:
                    publication = original_record_semantic_acceptance(
                        terminal_obj,
                        cw_obj,
                        agent_obj,
                        task_id,
                        return_publication=True,
                    )
                except TypeError as legacy_exc:
                    if "return_publication" not in str(legacy_exc):
                        raise
                    original_record_semantic_acceptance(
                        terminal_obj,
                        cw_obj,
                        agent_obj,
                        task_id,
                    )
                    publication = {}
''',
)

replace_once(
    "services/gateway/tests/test_coding_mission_acceptance_epoch.py",
    '''        if name == "coding_finish":
            self._agent._append_event(
                task_id,
                {
                    "type": "semantic_acceptance_review",
                    "cycle": int(self.cw.task.get("agent_cycle") or 0),
                    "accepted": True,
                },
            )
            return {"ok": True, "success": True}
''',
    '''        if name == "coding_finish":
            review_diff = epoch.mission_review_diff(
                self.cw, self._agent, task_id, self.cw.task
            )
            fingerprint = _TerminalHardening.semantic_acceptance_fingerprint(
                self.cw.task, diff_text=review_diff
            )
            self._agent._append_event(
                task_id,
                {
                    "type": "semantic_acceptance_review",
                    "cycle": int(self.cw.task.get("agent_cycle") or 0),
                    "accepted": True,
                    "fingerprint": fingerprint,
                },
            )
            return {
                "ok": True,
                "success": True,
                "_semantic_acceptance_review_identity": {
                    "fingerprint": fingerprint,
                    "cycle": int(self.cw.task.get("agent_cycle") or 0),
                },
            }
''',
)

replace_once(
    "services/gateway/tests/test_coding_mission_acceptance_epoch.py",
    '''    assert finish["ok"] is True
    assert finish["success"] is True
    assert cw.task[epoch.KEY]["status"] == "semantic_accepted"
''',
    '''    assert finish["ok"] is True
    assert finish["success"] is True
    assert "_semantic_acceptance_review_identity" not in finish
    assert cw.task[epoch.KEY]["status"] == "semantic_accepted"
''',
)

review_binding = Path(
    "services/gateway/tests/test_coding_mission_acceptance_epoch_review_binding.py"
)
text = review_binding.read_text()
marker = "\n\ndef test_acceptance_recorder_publishes_only_matching_review_fingerprint(monkeypatch) -> None:\n"
if text.count(marker) != 1:
    raise SystemExit("review binding insertion marker mismatch")
test = '''

def test_explicit_finish_review_identity_beats_later_stale_shared_event(monkeypatch) -> None:
    cw = _MemoryCW(_task("fp:reviewed-diff"))
    cw.task["agent_events"].append(
        {
            "type": "semantic_acceptance_review",
            "cycle": 4,
            "accepted": True,
            "fingerprint": "fp:stale-diff",
        }
    )
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )
    agent = SimpleNamespace()

    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, _task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": "reviewed-head",
            "diff_sha256": "reviewed-diff-sha",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "reviewed-diff",
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        agent,
        "code_epoch_review_binding",
        reviewed_fingerprint="fp:reviewed-diff",
        reviewed_cycle=4,
    )

    assert published is True
    assert cw.task[epoch.KEY]["accepted_fingerprint"] == "fp:reviewed-diff"
'''
review_binding.write_text(text.replace(marker, test + marker, 1))

integrity = Path(
    "services/gateway/tests/test_coding_semantic_acceptance_contract_integrity.py"
)
text = integrity.read_text()
marker = "\n\ndef test_frozen_contract_missing_review_fingerprint_is_logged_and_blocked(caplog) -> None:\n"
if text.count(marker) != 1:
    raise SystemExit("contract integrity insertion marker mismatch")
test = '''

def test_unfrozen_migrated_review_missing_fingerprint_is_logged_and_blocked(caplog) -> None:
    state = {"calls": 0}

    def latest_accepted_review(_task):
        return {"accepted": True, "fingerprint": "", "cycle": 8}

    def mission_review_diff(_cw, _agent, _task_id, _task):
        return "review-diff"

    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):
        state["calls"] += 1

    epoch_obj = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(
        task={
            "id": "code_unfrozen_missing_review_fp",
            "prompt": "Do the work.",
            "agent_cycle": 8,
        },
        epoch=epoch_obj,
    )

    with caplog.at_level(logging.WARNING, logger=contract.__name__):
        epoch_obj._record_semantic_acceptance(
            terminal, cw, agent, "code_unfrozen_missing_review_fp"
        )

    assert state["calls"] == 0
    assert "latest accepted review has no acceptance fingerprint" in caplog.text
'''
integrity.write_text(text.replace(marker, test + marker, 1))

for path_s in (
    "services/gateway/app/coding_agent_guarded.py",
    "services/gateway/app/coding_mission_acceptance_epoch.py",
    "services/gateway/app/coding_semantic_acceptance_contract.py",
    "services/gateway/tests/test_coding_mission_acceptance_epoch.py",
    "services/gateway/tests/test_coding_mission_acceptance_epoch_review_binding.py",
    "services/gateway/tests/test_coding_semantic_acceptance_contract_integrity.py",
):
    path = Path(path_s)
    compile(path.read_text(), str(path), "exec")
