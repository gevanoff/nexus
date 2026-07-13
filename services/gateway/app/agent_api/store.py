from __future__ import annotations

import secrets
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app import coding_workspace as cw
from app.agent_api.auth import AgentAuthContext
from app.agent_api.errors import ApiError
from app.agent_api.pagination import paginate
from app.config import S


_WORKSPACE_STATES = {"created", "running", "stopped", "archived", "error"}
_TASK_STATES = {"pending", "running", "completed", "failed", "cancelled", "deleted"}
_PRIORITIES = {"low", "normal", "high", "urgent"}
_ACTIVE_AGENT_STATES = {"queued", "running", "stopping", "pausing"}


def _now() -> float:
    return time.time()


def _iso(timestamp: Any) -> str:
    try:
        value = float(timestamp)
    except Exception:
        value = _now()
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _workspace_meta(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("agent_api_workspace")
    return value if isinstance(value, dict) else {}


def _workspace_state(task: dict[str, Any]) -> str:
    meta = _workspace_meta(task)
    state = str(meta.get("status") or "").strip().lower()
    if state in _WORKSPACE_STATES:
        return state
    if str(task.get("status") or "").strip().lower() == "error":
        return "error"
    return "created"


def _task_records(task: dict[str, Any]) -> list[dict[str, Any]]:
    records = task.get("agent_api_tasks")
    return [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _artifact_records(task: dict[str, Any]) -> list[dict[str, Any]]:
    records = task.get("agent_api_artifacts")
    return [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    counts = {state: 0 for state in ["pending", "running", "completed", "failed", "cancelled"]}
    for item in _task_records(task):
        state = str(item.get("status") or "pending").strip().lower()
        if state in counts:
            counts[state] += 1
    total = sum(counts.values())
    complete = counts["completed"] + counts["failed"] + counts["cancelled"]
    return {
        "total": total,
        **counts,
        "percent_complete": round((complete / total) * 100, 1) if total else 0.0,
    }


def _workspace_view(task: dict[str, Any], *, detail: bool = True) -> dict[str, Any]:
    meta = _workspace_meta(task)
    name = str(meta.get("name") or task.get("branch_name") or task.get("id") or "Workspace").strip()
    description = str(meta.get("description") or "") if "description" in meta else str(task.get("prompt") or "")
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    result: dict[str, Any] = {
        "id": str(task.get("id") or ""),
        "name": name,
        "description": description,
        "status": _workspace_state(task),
        "metadata": metadata,
        "created_at": _iso(task.get("created_at")),
        "updated_at": _iso(task.get("updated_at")),
    }
    if detail:
        result.update(
            {
                "task_summary": _task_summary(task),
                "repository": {
                    "url": cw.redact_repo_url(str(task.get("repo_url") or "")),
                    "base_branch": str(task.get("base_branch") or ""),
                    "branch_name": str(task.get("branch_name") or ""),
                },
                "runtime": {
                    "workspace_status": str(task.get("status") or ""),
                    "agent_status": str(task.get("agent_status") or "idle"),
                },
            }
        )
    return result


def _task_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "workspace_id": str(item.get("workspace_id") or ""),
        "instruction": str(item.get("instruction") or ""),
        "context": item.get("context"),
        "status": str(item.get("status") or "pending"),
        "priority": str(item.get("priority") or "normal"),
        "max_retries": int(item.get("max_retries") or 0),
        "retry_count": int(item.get("retry_count") or 0),
        "created_at": _iso(item.get("created_at")),
        "updated_at": _iso(item.get("updated_at")),
    }


def _artifact_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "workspace_id": str(item.get("workspace_id") or ""),
        "task_id": str(item.get("task_id") or "") or None,
        "filename": str(item.get("filename") or "artifact"),
        "mime_type": str(item.get("mime_type") or "application/octet-stream"),
        "size_bytes": int(item.get("size_bytes") or 0),
        "created_at": _iso(item.get("created_at")),
    }


def _ensure_access(task: dict[str, Any], auth: AgentAuthContext) -> None:
    workspace_id = str(task.get("id") or "")
    if auth.workspace_id and auth.workspace_id != workspace_id:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "Workspace not found")
    try:
        owner_user_id = int(task.get("owner_user_id"))
    except Exception:
        owner_user_id = -1
    if owner_user_id != auth.user_id:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "Workspace not found")


def _ensure_mutable(task: dict[str, Any]) -> None:
    if _workspace_state(task) == "archived":
        raise ApiError(409, "WORKSPACE_ARCHIVED", "Archived workspaces cannot be modified")


def load_workspace(workspace_id: str, auth: AgentAuthContext) -> dict[str, Any]:
    task = cw.load_task(workspace_id)
    _ensure_access(task, auth)
    return task


def initialize_workspace(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    name: str,
    description: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    def apply(task: dict[str, Any]) -> None:
        _ensure_access(task, auth)
        task["agent_api_workspace"] = {
            "name": str(name).strip(),
            "description": str(description or ""),
            "metadata": dict(metadata or {}),
            "status": "created" if str(task.get("status") or "") != "error" else "error",
        }
        task.setdefault("agent_api_tasks", [])
        task.setdefault("agent_api_artifacts", [])

    task = cw.mutate_task(workspace_id, apply)
    return _workspace_view(task)


def list_workspaces(
    auth: AgentAuthContext,
    *,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    requested_status = str(status or "").strip().lower()
    if requested_status == "active":
        requested_status = "running"
    if requested_status and requested_status not in _WORKSPACE_STATES:
        raise ApiError(
            400,
            "INVALID_STATUS",
            "Workspace status is invalid",
            details={"allowed": sorted(_WORKSPACE_STATES)},
        )

    candidates: list[dict[str, Any]] = []
    if auth.workspace_id:
        try:
            candidates = [load_workspace(auth.workspace_id, auth)]
        except Exception:
            candidates = []
    else:
        for path in cw.tasks_dir().glob("code_*.json"):
            workspace_id = path.stem
            if not workspace_id:
                continue
            try:
                task = cw.load_task(workspace_id)
                _ensure_access(task, auth)
            except Exception:
                continue
            candidates.append(task)

    if requested_status:
        candidates = [task for task in candidates if _workspace_state(task) == requested_status]
    page, next_cursor = paginate(
        candidates,
        limit=limit,
        cursor=cursor,
        timestamp=lambda task: float(task.get("updated_at") or task.get("created_at") or 0),
        item_id=lambda task: str(task.get("id") or ""),
    )
    return {"items": [_workspace_view(task, detail=False) for task in page], "next_cursor": next_cursor}


def get_workspace(workspace_id: str, auth: AgentAuthContext) -> dict[str, Any]:
    return _workspace_view(load_workspace(workspace_id, auth))


def update_workspace(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    updates: dict[str, Any],
) -> dict[str, Any]:
    def apply(task: dict[str, Any]) -> None:
        _ensure_access(task, auth)
        meta = dict(_workspace_meta(task))
        if _workspace_state(task) == "archived":
            raise ApiError(409, "WORKSPACE_ARCHIVED", "Archived workspaces cannot be updated")
        for field in ["name", "description", "metadata"]:
            if field in updates:
                meta[field] = updates[field]
        task["agent_api_workspace"] = meta

    return _workspace_view(cw.mutate_task(workspace_id, apply))


def transition_workspace(workspace_id: str, auth: AgentAuthContext, state: str) -> dict[str, Any]:
    if state not in {"running", "stopped", "archived"}:
        raise ValueError(f"unsupported workspace transition: {state}")

    def apply(task: dict[str, Any]) -> None:
        _ensure_access(task, auth)
        current = _workspace_state(task)
        if current == "archived":
            if state == "archived":
                return
            raise ApiError(409, "WORKSPACE_ARCHIVED", "Archived workspaces cannot change state")
        if state == "running" and str(task.get("status") or "").strip().lower() == "error":
            raise ApiError(409, "WORKSPACE_NOT_READY", "Workspace initialization failed")
        meta = dict(_workspace_meta(task))
        meta["status"] = state
        if state == "archived":
            meta["archived_at"] = _now()
        task["agent_api_workspace"] = meta

    return _workspace_view(cw.mutate_task(workspace_id, apply))


def workspace_status(workspace_id: str, auth: AgentAuthContext) -> dict[str, Any]:
    task = load_workspace(workspace_id, auth)
    return {
        "id": str(task.get("id") or ""),
        "status": _workspace_state(task),
        "updated_at": _iso(task.get("updated_at")),
        "task_progress": _task_summary(task),
    }


def _find_task(task: dict[str, Any], task_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    for item in _task_records(task):
        if str(item.get("id") or "") != str(task_id):
            continue
        if str(item.get("status") or "") == "deleted" and not include_deleted:
            break
        return item
    raise ApiError(404, "TASK_NOT_FOUND", "Task not found")


def list_tasks(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    task = load_workspace(workspace_id, auth)
    requested_status = str(status or "").strip().lower()
    if requested_status and requested_status not in (_TASK_STATES - {"deleted"}):
        raise ApiError(400, "INVALID_STATUS", "Task status is invalid")
    records = [item for item in _task_records(task) if str(item.get("status") or "") != "deleted"]
    if requested_status:
        records = [item for item in records if str(item.get("status") or "") == requested_status]
    page, next_cursor = paginate(
        records,
        limit=limit,
        cursor=cursor,
        timestamp=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
        item_id=lambda item: str(item.get("id") or ""),
    )
    return {"items": [_task_view(item) for item in page], "next_cursor": next_cursor}


def create_task(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    instruction: str,
    context: Any,
    priority: str,
    max_retries: int,
) -> dict[str, Any]:
    created: dict[str, Any] = {}

    def apply(task: dict[str, Any]) -> None:
        _ensure_access(task, auth)
        _ensure_mutable(task)
        timestamp = _now()
        created.update(
            {
                "id": f"task_{secrets.token_hex(6)}",
                "workspace_id": workspace_id,
                "instruction": str(instruction).strip(),
                "context": context,
                "status": "pending",
                "priority": priority if priority in _PRIORITIES else "normal",
                "max_retries": int(max_retries),
                "retry_count": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        records = _task_records(task)
        records.append(dict(created))
        task["agent_api_tasks"] = records

    cw.mutate_task(workspace_id, apply)
    return _task_view(created)


def get_task(workspace_id: str, task_id: str, auth: AgentAuthContext) -> dict[str, Any]:
    workspace = load_workspace(workspace_id, auth)
    return _task_view(_find_task(workspace, task_id))


def update_task(
    workspace_id: str,
    task_id: str,
    auth: AgentAuthContext,
    *,
    updates: dict[str, Any],
) -> dict[str, Any]:
    updated: dict[str, Any] = {}

    def apply(workspace: dict[str, Any]) -> None:
        _ensure_access(workspace, auth)
        _ensure_mutable(workspace)
        item = _find_task(workspace, task_id)
        if "status" in updates:
            status = str(updates["status"] or "").strip().lower()
            if status not in (_TASK_STATES - {"deleted"}):
                raise ApiError(400, "INVALID_STATUS", "Task status is invalid")
            item["status"] = status
        if "priority" in updates:
            priority = str(updates["priority"] or "").strip().lower()
            if priority not in _PRIORITIES:
                raise ApiError(400, "INVALID_PRIORITY", "Task priority is invalid")
            item["priority"] = priority
        item["updated_at"] = _now()
        updated.update(item)

    cw.mutate_task(workspace_id, apply)
    return _task_view(updated)


def delete_task(workspace_id: str, task_id: str, auth: AgentAuthContext) -> None:
    def apply(workspace: dict[str, Any]) -> None:
        _ensure_access(workspace, auth)
        _ensure_mutable(workspace)
        item = _find_task(workspace, task_id)
        item["status"] = "deleted"
        item["deleted_at"] = _now()
        item["updated_at"] = item["deleted_at"]

    cw.mutate_task(workspace_id, apply)


def retry_task(workspace_id: str, task_id: str, auth: AgentAuthContext) -> dict[str, Any]:
    updated: dict[str, Any] = {}

    def apply(workspace: dict[str, Any]) -> None:
        _ensure_access(workspace, auth)
        _ensure_mutable(workspace)
        item = _find_task(workspace, task_id)
        retry_count = int(item.get("retry_count") or 0)
        max_retries = int(item.get("max_retries") or 0)
        if retry_count >= max_retries:
            raise ApiError(
                409,
                "MAX_RETRIES_EXCEEDED",
                "Task has exhausted its retry allowance",
                details={"retry_count": retry_count, "max_retries": max_retries},
            )
        if str(item.get("status") or "").strip().lower() not in {"failed", "cancelled"}:
            raise ApiError(409, "TASK_NOT_RETRYABLE", "Only failed or cancelled tasks can be retried")
        item["retry_count"] = retry_count + 1
        item["status"] = "pending"
        item["updated_at"] = _now()
        updated.update(item)

    cw.mutate_task(workspace_id, apply)
    return _task_view(updated)


def _execution_argv(command: str | list[str] | None, code: str | None, language: str | None) -> tuple[list[str], Path | None]:
    if command is not None:
        if isinstance(command, list):
            return [str(item) for item in command], None
        try:
            return shlex.split(command, posix=True), None
        except ValueError as exc:
            raise ApiError(400, "INVALID_COMMAND", f"Command could not be parsed: {exc}") from exc

    normalized = str(language or "").strip().lower()
    if normalized in {"python", "python3", "py"}:
        executable, suffix = "python3", ".py"
    elif normalized in {"javascript", "js", "node", "nodejs"}:
        executable, suffix = "node", ".js"
    else:
        raise ApiError(
            400,
            "UNSUPPORTED_LANGUAGE",
            "Only Python and JavaScript code execution is supported",
            details={"supported_languages": ["python", "javascript"]},
        )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="nexus-agent-exec-",
        suffix=suffix,
        delete=False,
    )
    try:
        handle.write(str(code or ""))
        path = Path(handle.name)
    finally:
        handle.close()
    return [executable, str(path)], path


def execute(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    command: str | list[str] | None,
    code: str | None,
    language: str | None,
    git_token_value: str | None,
) -> dict[str, Any]:
    workspace = load_workspace(workspace_id, auth)
    if _workspace_state(workspace) != "running":
        raise ApiError(409, "WORKSPACE_NOT_RUNNING", "Workspace must be started before execution")
    argv, temporary_path = _execution_argv(command, code, language)
    try:
        result = cw.run_task_command(
            workspace_id,
            argv=argv,
            git_token_value=git_token_value,
        )
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return {
        "exit_code": int(result.get("returncode") if result.get("returncode") is not None else 1),
        "stdout": str(result.get("stdout") or ""),
        "stderr": str(result.get("stderr") or ""),
        "duration_ms": int(result.get("duration_ms") or 0),
    }


def _artifact_root(workspace: dict[str, Any]) -> Path:
    raw_workspace_path = str(workspace.get("workspace_path") or "").strip()
    if not raw_workspace_path:
        raise ApiError(500, "WORKSPACE_STORAGE_ERROR", "Workspace storage path is missing")
    workspace_path = Path(raw_workspace_path).resolve()
    configured_root = cw.workspace_root().resolve()
    try:
        workspace_path.relative_to(configured_root)
    except ValueError as exc:
        raise ApiError(500, "WORKSPACE_STORAGE_ERROR", "Workspace storage path is invalid") from exc
    root = workspace_path.joinpath("artifacts").resolve()
    try:
        root.relative_to(workspace_path)
    except ValueError as exc:
        raise ApiError(500, "WORKSPACE_STORAGE_ERROR", "Artifact storage path is invalid") from exc
    return root


def artifact_max_bytes() -> int:
    try:
        return max(1_000, min(1_000_000_000, int(getattr(S, "CODING_ARTIFACT_MAX_BYTES", 50_000_000) or 50_000_000)))
    except Exception:
        return 50_000_000


async def save_artifact(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    upload: UploadFile,
    task_id: str | None,
) -> dict[str, Any]:
    workspace = load_workspace(workspace_id, auth)
    _ensure_mutable(workspace)
    linked_task_id = str(task_id or "").strip() or None
    if linked_task_id:
        _find_task(workspace, linked_task_id)
    filename = Path(str(upload.filename or "artifact")).name.strip()
    if not filename or filename in {".", ".."}:
        raise ApiError(400, "INVALID_FILENAME", "Artifact filename is invalid")

    artifact_id = f"artifact_{secrets.token_hex(8)}"
    root = _artifact_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    target = root.joinpath(artifact_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ApiError(500, "WORKSPACE_STORAGE_ERROR", "Artifact path is invalid") from exc

    total = 0
    limit = artifact_max_bytes()
    try:
        with target.open("xb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ApiError(
                        413,
                        "ARTIFACT_TOO_LARGE",
                        "Artifact exceeds the configured upload limit",
                        details={"max_bytes": limit},
                    )
                handle.write(chunk)
    except Exception:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise

    record = {
        "id": artifact_id,
        "workspace_id": workspace_id,
        "task_id": linked_task_id,
        "filename": filename,
        "mime_type": str(upload.content_type or "application/octet-stream"),
        "size_bytes": total,
        "storage_name": artifact_id,
        "created_at": _now(),
    }

    def apply(task: dict[str, Any]) -> None:
        _ensure_access(task, auth)
        records = _artifact_records(task)
        records.append(dict(record))
        task["agent_api_artifacts"] = records

    try:
        cw.mutate_task(workspace_id, apply)
    except Exception:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return _artifact_view(record)


def list_artifacts(
    workspace_id: str,
    auth: AgentAuthContext,
    *,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    workspace = load_workspace(workspace_id, auth)
    records = _artifact_records(workspace)
    page, next_cursor = paginate(
        records,
        limit=limit,
        cursor=cursor,
        timestamp=lambda item: float(item.get("created_at") or 0),
        item_id=lambda item: str(item.get("id") or ""),
    )
    return {"items": [_artifact_view(item) for item in page], "next_cursor": next_cursor}


def get_artifact(
    workspace_id: str,
    artifact_id: str,
    auth: AgentAuthContext,
) -> tuple[dict[str, Any], Path]:
    workspace = load_workspace(workspace_id, auth)
    record = None
    for item in _artifact_records(workspace):
        if str(item.get("id") or "") == str(artifact_id):
            record = item
            break
    if record is None:
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "Artifact not found")
    root = _artifact_root(workspace)
    path = root.joinpath(str(record.get("storage_name") or "")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "Artifact not found") from exc
    if not path.is_file():
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "Artifact file is missing")
    return _artifact_view(record), path


def agent_is_active(workspace: dict[str, Any]) -> bool:
    return str(workspace.get("agent_status") or "").strip().lower() in _ACTIVE_AGENT_STATES
