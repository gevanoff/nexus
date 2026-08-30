from pathlib import Path


def replace_once(path_s: str, old: str, new: str, *, count: int = 1) -> None:
    path = Path(path_s)
    text = path.read_text()
    actual = text.count(old)
    if actual != count:
        raise SystemExit(f"{path_s}: expected {count} marker(s), got {actual}: {old[:180]!r}")
    path.write_text(text.replace(old, new))


def replace_span(path_s: str, start: str, end: str, new: str, *, include_end: bool = True) -> None:
    path = Path(path_s)
    text = path.read_text()
    if text.count(start) != 1:
        raise SystemExit(f"{path_s}: start marker count={text.count(start)}: {start!r}")
    start_i = text.index(start)
    end_i = text.index(end, start_i)
    if include_end:
        end_i += len(end)
    path.write_text(text[:start_i] + new + text[end_i:])


def replace_in_function(path_s: str, func_name: str, old: str, new: str, *, count: int = 1) -> None:
    path = Path(path_s)
    text = path.read_text()
    marker = f"def {func_name}("
    start = text.index(marker)
    next_def = text.find("\ndef ", start + len(marker))
    end = len(text) if next_def < 0 else next_def + 1
    block = text[start:end]
    actual = block.count(old)
    if actual != count:
        raise SystemExit(
            f"{path_s}:{func_name}: expected {count} marker(s), got {actual}: {old[:180]!r}"
        )
    block = block.replace(old, new)
    path.write_text(text[:start] + block + text[end:])


epoch = "services/gateway/app/coding_mission_acceptance_epoch.py"
contract = "services/gateway/app/coding_semantic_acceptance_contract.py"

# Production: publication authority is a complete invocation-local identity.
replace_once(
    epoch,
    "def _mapping(value: Any) -> Mapping[str, Any]:\n    return value if isinstance(value, Mapping) else {}\n\n\ndef _float(value: Any) -> float:\n",
    "def _mapping(value: Any) -> Mapping[str, Any]:\n    return value if isinstance(value, Mapping) else {}\n\n\ndef _consume_semantic_review_identity(\n    value: Any,\n) -> tuple[Dict[str, Any], Mapping[str, Any]]:\n    result = dict(value) if isinstance(value, Mapping) else {}\n    identity = _mapping(\n        result.pop(\"_semantic_acceptance_review_identity\", {})\n    )\n    return result, identity\n\n\ndef _float(value: Any) -> float:\n",
)
replace_span(
    epoch,
    "    initial = cw.load_task(task_id)\n    explicit_review_identity =",
    "    max_attempts = 2\n",
    "    bound_fingerprint = str(reviewed_fingerprint or \"\").strip()\n"
    "    if not bound_fingerprint or reviewed_cycle is None:\n"
    "        return {} if return_publication else False\n"
    "    bound_cycle = _int(reviewed_cycle)\n"
    "    initial = cw.load_task(task_id)\n"
    "    if _int(initial.get(\"agent_cycle\")) != bound_cycle:\n"
    "        return {} if return_publication else False\n"
    "    reviewed_fingerprint = bound_fingerprint\n"
    "    reviewed_cycle = bound_cycle\n"
    "    max_attempts = 2\n",
)
replace_once(
    epoch,
    "        def apply(latest: Dict[str, Any]) -> None:\n"
    "            nonlocal published, published_generation\n"
    "            current = dict(_mapping(latest.get(KEY)))\n",
    "        def apply(latest: Dict[str, Any]) -> None:\n"
    "            nonlocal published, published_generation\n"
    "            if _int(latest.get(\"agent_cycle\")) != reviewed_cycle:\n"
    "                return\n"
    "            current = dict(_mapping(latest.get(KEY)))\n",
)
replace_span(
    epoch,
    "        try:\n            before_task = cw.load_task(task_id)\n        except Exception:\n",
    "\n\n        forced_state = forced_action.active_state(before_task)",
    "        try:\n"
    "            before_task = cw.load_task(task_id)\n"
    "        except Exception:\n"
    "            delegated, _ = _consume_semantic_review_identity(\n"
    "                original_run_tool(\n"
    "                    task_id,\n"
    "                    name,\n"
    "                    args,\n"
    "                    git_token_value=git_token_value,\n"
    "                )\n"
    "            )\n"
    "            return delegated\n\n"
    "        forced_state = forced_action.active_state(before_task)",
)
replace_once(
    epoch,
    "        result = dict(\n"
    "            original_run_tool(\n"
    "                task_id,\n"
    "                name,\n"
    "                args,\n"
    "                git_token_value=git_token_value,\n"
    "            )\n"
    "        )\n"
    "        review_identity = _mapping(\n"
    "            result.pop(\"_semantic_acceptance_review_identity\", {})\n"
    "        )\n",
    "        result, review_identity = _consume_semantic_review_identity(\n"
    "            original_run_tool(\n"
    "                task_id,\n"
    "                name,\n"
    "                args,\n"
    "                git_token_value=git_token_value,\n"
    "            )\n"
    "        )\n",
)
replace_once(
    contract,
    "    if callable(original_record_semantic_acceptance) and callable(latest_accepted_review):\n",
    "    if callable(original_record_semantic_acceptance):\n",
)
replace_span(
    contract,
    "            if terminal_obj is not terminal_hardening or cw_obj is not cw or agent_obj is not agent:\n",
    "            if not reviewed_fingerprint:\n",
    "            if terminal_obj is not terminal_hardening or cw_obj is not cw or agent_obj is not agent:\n"
    "                return original_record_semantic_acceptance(\n"
    "                    terminal_obj,\n"
    "                    cw_obj,\n"
    "                    agent_obj,\n"
    "                    task_id,\n"
    "                    reviewed_fingerprint=reviewed_fingerprint,\n"
    "                    reviewed_cycle=reviewed_cycle,\n"
    "                    return_publication=return_publication,\n"
    "                )\n\n"
    "            before = cw_obj.load_task(task_id)\n"
    "            reviewed_fingerprint = str(reviewed_fingerprint or \"\").strip()\n"
    "            if not reviewed_fingerprint or reviewed_cycle is None:\n",
)
replace_once(
    contract,
    "                message = (\n"
    "                    \"Refusing to record semantic acceptance because the latest accepted review \"\n"
    "                    \"has no acceptance fingerprint.\"\n"
    "                )\n",
    "                message = (\n"
    "                    \"Refusing to record semantic acceptance because no complete \"\n"
    "                    \"invocation-local review identity was supplied.\"\n"
    "                )\n",
)
replace_once(
    contract,
    "                except Exception:\n"
    "                    pass\n"
    "                return\n\n"
    "            current_fingerprint = current_review_fingerprint(\n",
    "                except Exception:\n"
    "                    pass\n"
    "                return\n\n"
    "            effective_reviewed_cycle = int(reviewed_cycle)\n"
    "            if int(before.get(\"agent_cycle\") or 0) != effective_reviewed_cycle:\n"
    "                return\n\n"
    "            current_fingerprint = current_review_fingerprint(\n",
)
replace_span(
    contract,
    "            try:\n                publication = original_record_semantic_acceptance(\n",
    "                    publication = {}\n",
    "            publication = original_record_semantic_acceptance(\n"
    "                terminal_obj,\n"
    "                cw_obj,\n"
    "                agent_obj,\n"
    "                task_id,\n"
    "                reviewed_fingerprint=reviewed_fingerprint,\n"
    "                reviewed_cycle=effective_reviewed_cycle,\n"
    "                return_publication=True,\n"
    "            )\n",
)

# Owner tests now use explicit authority except tests that deliberately omit it.
review_binding = "services/gateway/tests/test_coding_mission_acceptance_epoch_review_binding.py"
old_call = (
    "    _owner_record_semantic_acceptance()(\n"
    "        terminal, cw, agent, \"code_epoch_review_binding\"\n"
    "    )\n"
)
new_call = (
    "    _owner_record_semantic_acceptance()(\n"
    "        terminal,\n"
    "        cw,\n"
    "        agent,\n"
    "        \"code_epoch_review_binding\",\n"
    "        reviewed_fingerprint=\"fp:reviewed-diff\",\n"
    "        reviewed_cycle=4,\n"
    "    )\n"
)
replace_in_function(review_binding, "test_acceptance_recorder_refuses_live_fingerprint_not_bound_to_review", old_call, new_call)
replace_in_function(review_binding, "test_acceptance_recorder_publishes_only_matching_review_fingerprint", old_call, new_call)
path = Path(review_binding)
path.write_text(path.read_text() + '''\n\ndef test_owner_recorder_requires_complete_invocation_identity() -> None:\n    terminal = SimpleNamespace(\n        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"\n    )\n    recorder = _owner_record_semantic_acceptance()\n    for kwargs in (\n        {},\n        {"reviewed_fingerprint": "fp:reviewed-diff"},\n        {"reviewed_cycle": 4},\n    ):\n        cw = _MemoryCW(_task("fp:reviewed-diff"))\n        published = recorder(\n            terminal, cw, SimpleNamespace(), "code_epoch_review_binding", **kwargs\n        )\n        assert published is False\n        assert cw.mutations == 0\n        assert cw.task[epoch.KEY]["status"] == "pending"\n        assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""\n\n\ndef test_owner_recorder_rechecks_cycle_inside_atomic_publication(monkeypatch) -> None:\n    class _CycleRaceCW(_MemoryCW):\n        def mutate_task(self, _task_id: str, apply):\n            self.mutations += 1\n            latest = dict(self.task)\n            latest["agent_cycle"] = 5\n            apply(latest)\n            self.task = latest\n            return dict(latest)\n\n    cw = _CycleRaceCW(_task("fp:reviewed-diff"))\n    terminal = SimpleNamespace(\n        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"\n    )\n    monkeypatch.setattr(\n        epoch,\n        "mission_delta_state",\n        lambda _cw, _task_id, _task: {\n            "ok": True, "has_delta": True, "base_head": "base",\n            "current_head": "reviewed-head", "diff_sha256": "reviewed-diff-sha",\n        },\n    )\n    monkeypatch.setattr(\n        epoch, "mission_review_diff",\n        lambda _cw, _agent, _task_id, _task: "reviewed-diff",\n    )\n    published = _owner_record_semantic_acceptance()(\n        terminal, cw, SimpleNamespace(), "code_epoch_review_binding",\n        reviewed_fingerprint="fp:reviewed-diff", reviewed_cycle=4,\n    )\n    assert published is False\n    assert cw.mutations == 1\n    assert cw.task["agent_cycle"] == 5\n    assert cw.task[epoch.KEY]["status"] == "pending"\n    assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""\n''')

publication_cas = "services/gateway/tests/test_coding_mission_acceptance_epoch_publication_cas.py"
call = "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        cw.task[\"id\"],\n    )\n"
call_new = "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        cw.task[\"id\"],\n        reviewed_fingerprint=\"fp:reviewed-a\",\n        reviewed_cycle=7,\n    )\n"
replace_once(publication_cas, call, call_new, count=2)

publication_retry = "services/gateway/tests/test_coding_mission_acceptance_epoch_publication_retry.py"
call = "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        \"code_epoch_publication_retry\",\n    )\n"
call_new = "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        \"code_epoch_publication_retry\",\n        reviewed_fingerprint=\"fp:current-diff\",\n        reviewed_cycle=4,\n    )\n"
replace_once(publication_retry, call, call_new, count=5)

generation = "services/gateway/tests/test_coding_mission_acceptance_publication_generation.py"
replace_once(generation, "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        cw.task[\"id\"],\n    )\n", "    published = epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        SimpleNamespace(),\n        cw.task[\"id\"],\n        reviewed_fingerprint=\"fp:y\",\n        reviewed_cycle=5,\n    )\n")
replace_in_function(generation, "test_stale_cleanup_preserves_new_same_fingerprint_publication", '    state = {"review": {"accepted": True, "fingerprint": ""}}\n', '    state = {"review": {"accepted": True, "fingerprint": "", "cycle": 5}}\n')
replace_in_function(generation, "test_stale_cleanup_preserves_new_same_fingerprint_publication", '            "repo_version": 1,\n', '            "repo_version": 1,\n            "agent_cycle": 5,\n')
replace_in_function(generation, "test_stale_cleanup_preserves_new_same_fingerprint_publication", "        *,\n        return_publication=False,\n    ):\n        before = cw_obj.load_task(task_id)\n        fingerprint = terminal_obj.semantic_acceptance_fingerprint(\n", "        *,\n        reviewed_fingerprint=\"\",\n        reviewed_cycle=None,\n        return_publication=False,\n    ):\n        before = cw_obj.load_task(task_id)\n        if not reviewed_fingerprint or reviewed_cycle is None:\n            return {} if return_publication else False\n        if int(before.get(\"agent_cycle\") or 0) != int(reviewed_cycle):\n            return {} if return_publication else False\n        fingerprint = terminal_obj.semantic_acceptance_fingerprint(\n")
replace_in_function(generation, "test_stale_cleanup_preserves_new_same_fingerprint_publication", "        )\n\n        def apply(latest):\n", "        )\n        if fingerprint != reviewed_fingerprint:\n            return {} if return_publication else False\n\n        def apply(latest):\n")
replace_in_function(generation, "test_stale_cleanup_preserves_new_same_fingerprint_publication", "    epoch_stub._record_semantic_acceptance(\n        terminal,\n        cw,\n        agent,\n        \"code_cleanup_generation_aba\",\n    )\n", "    epoch_stub._record_semantic_acceptance(\n        terminal,\n        cw,\n        agent,\n        \"code_cleanup_generation_aba\",\n        reviewed_fingerprint=state[\"review\"][\"fingerprint\"],\n        reviewed_cycle=5,\n    )\n")

integrity = "services/gateway/tests/test_coding_semantic_acceptance_contract_integrity.py"
replace_in_function(integrity, "test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint", "    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id):\n        state[\"calls\"] += 1\n", "    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):\n        state[\"calls\"] += 1\n")
replace_in_function(integrity, "test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint", '        task={"id": "code_accept_race", "prompt": "Do the work."},\n', '        task={"id": "code_accept_race", "prompt": "Do the work.", "agent_cycle": 3},\n')
replace_in_function(integrity, "test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint", '    epoch._record_semantic_acceptance(terminal, cw, agent, "code_accept_race")\n', '    epoch._record_semantic_acceptance(\n        terminal, cw, agent, "code_accept_race",\n        reviewed_fingerprint="stale", reviewed_cycle=3,\n    )\n', count=1)
replace_in_function(integrity, "test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint", '    epoch._record_semantic_acceptance(terminal, cw, agent, "code_accept_race")\n', '    epoch._record_semantic_acceptance(\n        terminal, cw, agent, "code_accept_race",\n        reviewed_fingerprint=state["review"]["fingerprint"], reviewed_cycle=3,\n    )\n', count=1)
replace_in_function(integrity, "test_unfrozen_migrated_review_missing_fingerprint_is_logged_and_blocked", '    assert "latest accepted review has no acceptance fingerprint" in caplog.text\n', '    assert "no complete invocation-local review identity was supplied" in caplog.text\n')
replace_in_function(integrity, "test_frozen_contract_missing_review_fingerprint_is_logged_and_blocked", '    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id):\n        state["calls"] += 1\n', '    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):\n        state["calls"] += 1\n')
replace_in_function(integrity, "test_frozen_contract_missing_review_fingerprint_is_logged_and_blocked", '    assert "latest accepted review has no acceptance fingerprint" in caplog.text\n', '    assert "no complete invocation-local review identity was supplied" in caplog.text\n')
for func_name, task_id, cycle in (("test_mission_acceptance_clears_state_if_workspace_changes_during_record", "code_accept_post_race", 4), ("test_stale_cleanup_does_not_erase_newer_concurrent_acceptance", "code_accept_cleanup_race", 4)):
    replace_in_function(integrity, func_name, '    state = {"review": {"accepted": True, "fingerprint": ""}}\n', f'    state = {{"review": {{"accepted": True, "fingerprint": "", "cycle": {cycle}}}}}\n')
    replace_in_function(integrity, func_name, '            "repo_version": 1,\n', f'            "repo_version": 1,\n            "agent_cycle": {cycle},\n')
    replace_in_function(integrity, func_name, "        *,\n        return_publication=False,\n    ):\n        before = cw.load_task(task_id)\n        accepted_fp = terminal.semantic_acceptance_fingerprint(\n", "        *,\n        reviewed_fingerprint=\"\",\n        reviewed_cycle=None,\n        return_publication=False,\n    ):\n        before = cw.load_task(task_id)\n        if not reviewed_fingerprint or reviewed_cycle is None:\n            return {} if return_publication else False\n        if int(before.get(\"agent_cycle\") or 0) != int(reviewed_cycle):\n            return {} if return_publication else False\n        accepted_fp = terminal.semantic_acceptance_fingerprint(\n")
    replace_in_function(integrity, func_name, "        )\n        generation = int(\n", "        )\n        if accepted_fp != reviewed_fingerprint:\n            return {} if return_publication else False\n        generation = int(\n")
    replace_in_function(integrity, func_name, f'    epoch._record_semantic_acceptance(terminal, cw, agent, "{task_id}")\n', f'    epoch._record_semantic_acceptance(\n        terminal, cw, agent, "{task_id}",\n        reviewed_fingerprint=state["review"]["fingerprint"], reviewed_cycle={cycle},\n    )\n')

post_record = "services/gateway/tests/test_coding_semantic_acceptance_contract_post_record_race.py"
replace_once(post_record, '    state = {"review": {"accepted": True, "fingerprint": ""}}\n', '    state = {"review": {"accepted": True, "fingerprint": "", "cycle": 4}}\n')
replace_once(post_record, '            "repo_version": 1,\n', '            "repo_version": 1,\n            "agent_cycle": 4,\n')
replace_once(post_record, "        *,\n        return_publication=False,\n    ):\n        before = cw_obj.load_task(task_id)\n        stale_fingerprint = terminal_obj.semantic_acceptance_fingerprint(\n", "        *,\n        reviewed_fingerprint=\"\",\n        reviewed_cycle=None,\n        return_publication=False,\n    ):\n        before = cw_obj.load_task(task_id)\n        if not reviewed_fingerprint or reviewed_cycle is None:\n            return {} if return_publication else False\n        if int(before.get(\"agent_cycle\") or 0) != int(reviewed_cycle):\n            return {} if return_publication else False\n        stale_fingerprint = terminal_obj.semantic_acceptance_fingerprint(\n")
replace_once(post_record, "        )\n        generation = int(\n", "        )\n        if stale_fingerprint != reviewed_fingerprint:\n            return {} if return_publication else False\n        generation = int(\n")
replace_once(post_record, "    epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        agent,\n        \"code_accept_post_record_load_race\",\n    )\n", "    epoch._record_semantic_acceptance(\n        terminal,\n        cw,\n        agent,\n        \"code_accept_post_record_load_race\",\n        reviewed_fingerprint=state[\"review\"][\"fingerprint\"],\n        reviewed_cycle=4,\n    )\n")

# Exception-path sanitization regression.
epoch_tests = "services/gateway/tests/test_coding_mission_acceptance_epoch.py"
path = Path(epoch_tests)
text = path.read_text()
marker = "\n\ndef test_unaccepted_inherited_delta_does_not_relax_finalization_contract():\n"
if text.count(marker) != 1:
    raise SystemExit("epoch test insertion marker mismatch")
new_test = '''\n\ndef test_initial_load_failure_still_sanitizes_private_review_identity():\n    cw = _CW()\n    agent = _Agent(cw)\n    guarded = _Guarded(agent, cw)\n    epoch.install(agent, guarded, cw, _ForcedAction(), _TerminalHardening())\n    original_load = cw.load_task\n    calls = {"count": 0}\n\n    def fail_once(task_id):\n        calls["count"] += 1\n        if calls["count"] == 1:\n            raise RuntimeError("synthetic initial load failure")\n        return original_load(task_id)\n\n    cw.load_task = fail_once\n    result = guarded._run_tool_with_semantic_acceptance(\n        "code-test", "coding_finish", {}, git_token_value=None\n    )\n    assert result["ok"] is True\n    assert result["success"] is True\n    assert "_semantic_acceptance_review_identity" not in result\n'''
path.write_text(text.replace(marker, new_test + marker, 1))
