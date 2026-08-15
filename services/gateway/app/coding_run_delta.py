from __future__ import annotations

import difflib
from typing import Any, Dict


SCHEMA = "nexus_coding_run_delta.v1"
_MAX_BASELINE_UNTRACKED = 200


def _stdout(result: Dict[str, Any]) -> str:
    return str(result.get("stdout") or "").strip() if isinstance(result, dict) else ""


def _git(cw: Any, task_id: str, argv: list[str]) -> Dict[str, Any]:
    return cw.run_task_command(task_id, argv=argv, timeout_sec=30)


def _untracked_paths(cw: Any, task_id: str) -> tuple[list[str], str]:
    result = _git(cw, task_id, ["git", "ls-files", "--others", "--exclude-standard"])
    if not bool(result.get("ok")):
        return [], f"unable to enumerate untracked files: {result.get('error') or result.get('stderr') or 'git ls-files failed'}"
    paths = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    if len(paths) > _MAX_BASELINE_UNTRACKED:
        return [], f"semantic baseline has {len(paths)} untracked files; limit is {_MAX_BASELINE_UNTRACKED}"
    return paths, ""


def capture_baseline(cw: Any, task_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(task.get("agent_run_id") or "").strip()
    head = cw.git_head(task_id)
    head_commit = str(head.get("commit") or "").strip() if isinstance(head, dict) else ""

    snapshot = _git(cw, task_id, ["git", "stash", "create", "nexus-semantic-acceptance-baseline"])
    if not bool(snapshot.get("ok")):
        return {
            "schema": SCHEMA,
            "run_id": run_id,
            "tree_commit": "",
            "untracked_blobs": {},
            "error": str(snapshot.get("error") or snapshot.get("stderr") or "git stash create failed"),
        }
    tree_commit = _stdout(snapshot) or head_commit
    if not tree_commit:
        return {
            "schema": SCHEMA,
            "run_id": run_id,
            "tree_commit": "",
            "untracked_blobs": {},
            "error": "unable to identify a baseline tree commit",
        }

    untracked, error = _untracked_paths(cw, task_id)
    if error:
        return {
            "schema": SCHEMA,
            "run_id": run_id,
            "tree_commit": tree_commit,
            "untracked_blobs": {},
            "error": error,
        }

    blobs: Dict[str, str] = {}
    for path in untracked:
        hashed = _git(cw, task_id, ["git", "hash-object", "-w", "--", path])
        sha = _stdout(hashed)
        if not bool(hashed.get("ok")) or not sha:
            return {
                "schema": SCHEMA,
                "run_id": run_id,
                "tree_commit": tree_commit,
                "untracked_blobs": blobs,
                "error": f"unable to snapshot pre-existing untracked file: {path}",
            }
        blobs[path] = sha

    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "tree_commit": tree_commit,
        "untracked_blobs": blobs,
        "error": "",
    }


def ensure_baseline(cw: Any, task_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(task.get("agent_run_id") or "").strip()
    existing = task.get("agent_semantic_baseline") if isinstance(task.get("agent_semantic_baseline"), dict) else {}
    if str(existing.get("schema") or "") == SCHEMA and str(existing.get("run_id") or "") == run_id:
        return dict(existing)

    baseline = capture_baseline(cw, task_id, task)
    latest = cw.load_task(task_id)
    latest["agent_semantic_baseline"] = baseline
    cw.save_task(latest)
    return baseline


def _read_current(cw: Any, task_id: str, path: str) -> tuple[str, bool]:
    try:
        result = cw.read_file(task_id, path=path)
    except Exception:
        return "", False
    return str(result.get("content") or ""), True


def _blob_text(cw: Any, task_id: str, sha: str) -> tuple[str, bool]:
    result = _git(cw, task_id, ["git", "cat-file", "blob", sha])
    if not bool(result.get("ok")):
        return "", False
    return str(result.get("stdout") or ""), True


def _current_hash(cw: Any, task_id: str, path: str) -> str:
    result = _git(cw, task_id, ["git", "hash-object", "--", path])
    return _stdout(result) if bool(result.get("ok")) else ""


def _text_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}" if before else "/dev/null",
            tofile=f"b/{path}" if after else "/dev/null",
        )
    ).strip()


def run_delta_diff(cw: Any, agent: Any, task_id: str, task: Dict[str, Any]) -> str:
    baseline = task.get("agent_semantic_baseline") if isinstance(task.get("agent_semantic_baseline"), dict) else {}
    run_id = str(task.get("agent_run_id") or "").strip()
    if (
        str(baseline.get("schema") or "") != SCHEMA
        or str(baseline.get("run_id") or "") != run_id
        or str(baseline.get("error") or "")
    ):
        return ""

    tree_commit = str(baseline.get("tree_commit") or "").strip()
    if not tree_commit:
        return ""
    baseline_untracked = {
        str(path): str(sha)
        for path, sha in (baseline.get("untracked_blobs") or {}).items()
        if str(path) and str(sha)
    }

    argv = ["git", "diff", "--no-ext-diff", "--binary", tree_commit, "--", "."]
    argv.extend(f":(exclude,literal){path}" for path in sorted(baseline_untracked))
    tracked = _git(cw, task_id, argv)
    if not bool(tracked.get("ok")):
        return ""

    pieces: list[str] = []
    tracked_text = str(tracked.get("stdout") or "").strip()
    if tracked_text:
        pieces.append(tracked_text)

    current_untracked, error = _untracked_paths(cw, task_id)
    if error:
        return ""
    current_untracked_set = set(current_untracked)

    for path, old_sha in sorted(baseline_untracked.items()):
        current, exists = _read_current(cw, task_id, path)
        current_sha = _current_hash(cw, task_id, path) if exists else ""
        if exists and current_sha == old_sha:
            continue
        before, ok = _blob_text(cw, task_id, old_sha)
        if not ok:
            return ""
        rendered = _text_diff(path, before, current if exists else "")
        if rendered:
            pieces.append(rendered)

    for path in sorted(current_untracked_set.difference(baseline_untracked)):
        current, exists = _read_current(cw, task_id, path)
        if not exists:
            continue
        rendered = _text_diff(path, "", current)
        if rendered:
            pieces.append(rendered)

    return agent._clip_text("\n\n".join(pieces).strip(), 20_000)
