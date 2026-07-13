from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth as gateway_auth
from app import coding_workspace as cw
from app import user_store
from app.agent_api.auth import AgentToolCaller, DEFAULT_AGENT_SCOPES
from app.agent_api.errors import install_agent_api_error_handlers
from app.agent_api.routes import router
from app.agent_api.tool import execute_agent_api_tool
from app.config import S


@pytest.fixture()
def agent_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db_path = tmp_path / "users.sqlite"
    workspaces = tmp_path / "workspaces"
    tasks = tmp_path / "tasks"

    monkeypatch.setattr(S, "USER_DB_PATH", str(db_path))
    monkeypatch.setattr(S, "USER_AUTH_ENABLED", True)
    monkeypatch.setattr(S, "GATEWAY_BEARER_TOKEN", "static-test-token")
    monkeypatch.setattr(S, "GATEWAY_BEARER_TOKENS", "")
    monkeypatch.setattr(S, "GATEWAY_TOKEN_POLICIES_JSON", "")
    monkeypatch.setattr(S, "CODING_ALLOWED_COMMANDS", "git,python3,node")
    monkeypatch.setattr(S, "CODING_ARTIFACT_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(cw, "workspace_root", lambda: workspaces.resolve())
    monkeypatch.setattr(cw, "tasks_dir", lambda: tasks.resolve())
    gateway_auth._parse_token_policies.cache_clear()

    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(
        str(db_path),
        username="agent-owner",
        password="correct-horse-battery-staple",
        admin=False,
    )
    token = user_store.create_api_key(str(db_path), user_id=user.id, name="agent")

    def fake_create_task(
        *,
        repo_url,
        base_branch,
        branch_name,
        prompt,
        owner,
        owner_user_id=None,
        git_token_value=None,
        coding_model=None,
    ):
        del git_token_value
        cw._ensure_dirs()
        task_id = cw.new_task_id()
        workspace_path = cw.workspace_root().joinpath(task_id)
        repo_path = workspace_path.joinpath("repo")
        repo_path.mkdir(parents=True)
        timestamp = time.time()
        task = {
            "schema": cw.SCHEMA,
            "id": task_id,
            "status": "ready",
            "created_at": timestamp,
            "updated_at": timestamp,
            "owner": owner,
            "owner_user_id": owner_user_id,
            "repo_url": repo_url or "https://github.com/gevanoff/nexus.git",
            "base_branch": base_branch or "main",
            "branch_name": branch_name or f"agent-api/{task_id}",
            "prompt": prompt or "",
            "coding_model": coding_model or "",
            "workspace_path": str(workspace_path),
            "repo_path": str(repo_path),
            "commands": [],
            "project_plan": cw.normalize_project_plan({"goal": prompt or "", "items": []}),
            "agent_runs": [],
        }
        cw.save_task(task)
        return cw.public_task(task)

    monkeypatch.setattr(cw, "create_task", fake_create_task)

    app = FastAPI(title="Agent API Test", version="test")
    install_agent_api_error_handlers(app)
    app.include_router(router)
    client = TestClient(app)
    yield {
        "client": client,
        "headers": {"Authorization": f"Bearer {token['token']}"},
        "token": token,
        "user": user,
        "db_path": db_path,
    }
    client.close()
    gateway_auth._parse_token_policies.cache_clear()


def _create_workspace(api: dict[str, Any], name: str = "API workspace") -> dict[str, Any]:
    response = api["client"].post(
        "/api/v1/workspaces",
        headers=api["headers"],
        json={"name": name, "description": "External agent workspace", "metadata": {"source": "test"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _tool_caller(api: dict[str, Any], token: str | None = None) -> AgentToolCaller:
    resolved = user_store.get_user_by_api_key(
        str(api["db_path"]),
        token=token or api["token"]["token"],
        touch_last_used=False,
    )
    assert resolved is not None
    user, metadata = resolved
    return AgentToolCaller(user=user, token=metadata)


def _run_tool(
    api: dict[str, Any],
    operation: str,
    *,
    workspace_id: str | None = None,
    task_id: str | None = None,
    parameters: dict[str, Any] | None = None,
    caller: AgentToolCaller | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        execute_agent_api_tool(
            {
                "operation": operation,
                "workspace_id": workspace_id,
                "task_id": task_id,
                "parameters": parameters or {},
            },
            caller or _tool_caller(api),
        )
    )


def test_personal_token_format_health_auth_and_me(agent_api: dict[str, Any]) -> None:
    client = agent_api["client"]
    assert agent_api["token"]["token"].startswith("nxs_pat_")
    assert len(agent_api["token"]["token"]) == len("nxs_pat_") + 64

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    missing = client.get("/api/v1/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert missing.json()["error"]["request_id"].startswith("req_")

    static = client.get("/api/v1/me", headers={"Authorization": "Bearer static-test-token"})
    assert static.status_code == 403
    assert static.json()["error"]["code"] == "PERSONAL_TOKEN_REQUIRED"

    me = client.get("/api/v1/me", headers=agent_api["headers"])
    assert me.status_code == 200
    assert me.json()["token_type"] == "personal_access_token"
    assert me.json()["user_id"] == agent_api["user"].id
    assert me.json()["scopes"] == list(DEFAULT_AGENT_SCOPES)

    keys = user_store.list_api_keys(str(agent_api["db_path"]), user_id=agent_api["user"].id)
    assert keys[0]["last_used_ts"] is not None
    assert user_store.revoke_api_key(
        str(agent_api["db_path"]),
        user_id=agent_api["user"].id,
        key_id=agent_api["token"]["id"],
    )
    revoked = client.get("/api/v1/me", headers=agent_api["headers"])
    assert revoked.status_code == 403
    assert revoked.json()["error"]["code"] == "INVALID_TOKEN"


def test_scope_and_validation_errors_use_standard_envelope(agent_api: dict[str, Any]) -> None:
    limited = user_store.create_api_key(
        str(agent_api["db_path"]),
        user_id=agent_api["user"].id,
        name="read-only",
        policy={"scopes": ["workspaces:read"]},
    )
    headers = {"Authorization": f"Bearer {limited['token']}"}

    denied = agent_api["client"].post("/api/v1/workspaces", headers=headers, json={"name": "Denied"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert denied.json()["error"]["details"]["required_scope"] == "workspaces:write"

    invalid = agent_api["client"].post("/api/v1/workspaces", headers=agent_api["headers"], json={})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid.json()["error"]["details"]["errors"]

    blank = agent_api["client"].post(
        "/api/v1/workspaces",
        headers=agent_api["headers"],
        json={"name": "   "},
    )
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "VALIDATION_ERROR"

    missing_route = agent_api["client"].get("/api/v1/not-a-route", headers=agent_api["headers"])
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "NOT_FOUND"


def test_workspace_lifecycle_and_safe_execution(agent_api: dict[str, Any]) -> None:
    client = agent_api["client"]
    workspace = _create_workspace(agent_api)
    workspace_id = workspace["id"]
    assert workspace["status"] == "created"

    before_start = client.post(
        f"/api/v1/workspaces/{workspace_id}/execute",
        headers=agent_api["headers"],
        json={"code": "print('not yet')", "language": "python"},
    )
    assert before_start.status_code == 409
    assert before_start.json()["error"]["code"] == "WORKSPACE_NOT_RUNNING"

    started = client.post(f"/api/v1/workspaces/{workspace_id}/start", headers=agent_api["headers"])
    assert started.status_code == 200
    assert started.json()["status"] == "running"

    executed = client.post(
        f"/api/v1/workspaces/{workspace_id}/execute",
        headers=agent_api["headers"],
        json={"code": "print(6 * 7)", "language": "python"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["exit_code"] == 0
    assert executed.json()["stdout"].strip() == "42"

    shell = client.post(
        f"/api/v1/workspaces/{workspace_id}/execute",
        headers=agent_api["headers"],
        json={"command": "sh -c 'echo unsafe'"},
    )
    assert shell.status_code == 403
    assert shell.json()["error"]["code"] == "FORBIDDEN"

    patched = client.patch(
        f"/api/v1/workspaces/{workspace_id}",
        headers=agent_api["headers"],
        json={"name": "Renamed", "metadata": {"source": "patched"}},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"

    status = client.get(f"/api/v1/workspaces/{workspace_id}/status", headers=agent_api["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "running"
    active = client.get("/api/v1/workspaces?status=active", headers=agent_api["headers"])
    assert [item["id"] for item in active.json()["items"]] == [workspace_id]

    stopped = client.post(f"/api/v1/workspaces/{workspace_id}/stop", headers=agent_api["headers"])
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"

    deleted = client.delete(f"/api/v1/workspaces/{workspace_id}", headers=agent_api["headers"])
    assert deleted.status_code == 204
    archived = client.get(f"/api/v1/workspaces/{workspace_id}/status", headers=agent_api["headers"])
    assert archived.json()["status"] == "archived"


def test_task_crud_retry_and_cursor_pagination(agent_api: dict[str, Any]) -> None:
    client = agent_api["client"]
    workspace_id = _create_workspace(agent_api)["id"]
    task_ids = []
    for index in range(3):
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/tasks",
            headers=agent_api["headers"],
            json={
                "instruction": f"Task {index}",
                "context": {"index": index},
                "priority": "high" if index == 0 else "normal",
                "max_retries": 1,
            },
        )
        assert response.status_code == 201
        task_ids.append(response.json()["id"])
        time.sleep(0.002)

    first_page = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks?limit=2",
        headers=agent_api["headers"],
    )
    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 2
    assert first_page.json()["next_cursor"]
    second_page = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks",
        headers=agent_api["headers"],
        params={"limit": 2, "cursor": first_page.json()["next_cursor"]},
    )
    assert len(second_page.json()["items"]) == 1

    task_id = task_ids[0]
    patched = client.patch(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        headers=agent_api["headers"],
        json={"status": "failed", "priority": "urgent"},
    )
    assert patched.json()["status"] == "failed"
    assert patched.json()["priority"] == "urgent"

    retried = client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/retry",
        headers=agent_api["headers"],
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["retry_count"] == 1

    exhausted = client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/retry",
        headers=agent_api["headers"],
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["error"]["code"] == "MAX_RETRIES_EXCEEDED"

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        headers=agent_api["headers"],
    )
    assert deleted.status_code == 204
    missing = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        headers=agent_api["headers"],
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_artifact_upload_list_and_download(agent_api: dict[str, Any]) -> None:
    client = agent_api["client"]
    workspace_id = _create_workspace(agent_api)["id"]
    task = client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks",
        headers=agent_api["headers"],
        json={"instruction": "Create report"},
    ).json()

    uploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/artifacts",
        headers=agent_api["headers"],
        data={"task_id": task["id"]},
        files={"file": ("report.txt", b"artifact body\n", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact = uploaded.json()
    assert artifact["filename"] == "report.txt"
    assert artifact["task_id"] == task["id"]
    assert artifact["size_bytes"] == len(b"artifact body\n")

    listing = client.get(
        f"/api/v1/workspaces/{workspace_id}/artifacts",
        headers=agent_api["headers"],
    )
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [artifact["id"]]

    download = client.get(
        f"/api/v1/workspaces/{workspace_id}/artifacts/{artifact['id']}",
        headers=agent_api["headers"],
    )
    assert download.status_code == 200
    assert download.content == b"artifact body\n"
    assert "report.txt" in download.headers["content-disposition"]


def test_workspace_ownership_binding_and_schema(agent_api: dict[str, Any]) -> None:
    workspace = _create_workspace(agent_api, "Bound workspace")
    workspace_id = workspace["id"]
    other = user_store.create_user_with_admin(
        str(agent_api["db_path"]),
        username="other-agent",
        password="another-long-password",
        admin=False,
    )
    other_key = user_store.create_api_key(str(agent_api["db_path"]), user_id=other.id, name="other")
    other_headers = {"Authorization": f"Bearer {other_key['token']}"}
    hidden = agent_api["client"].get(f"/api/v1/workspaces/{workspace_id}", headers=other_headers)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    bound_key = user_store.create_api_key(
        str(agent_api["db_path"]),
        user_id=agent_api["user"].id,
        name="bound",
        policy={"workspace_id": workspace_id},
    )
    bound_headers = {"Authorization": f"Bearer {bound_key['token']}"}
    listing = agent_api["client"].get("/api/v1/workspaces", headers=bound_headers)
    assert [item["id"] for item in listing.json()["items"]] == [workspace_id]
    create_denied = agent_api["client"].post(
        "/api/v1/workspaces",
        headers=bound_headers,
        json={"name": "Another"},
    )
    assert create_denied.status_code == 403
    assert create_denied.json()["error"]["code"] == "WORKSPACE_TOKEN_RESTRICTED"

    schema = agent_api["client"].get("/api/v1/schema", headers=agent_api["headers"])
    assert schema.status_code == 200
    document = schema.json()
    assert document["openapi"] == "3.0.3"
    assert "/api/v1/workspaces" in document["paths"]
    assert document["components"]["securitySchemes"]["BearerAuth"]["bearerFormat"] == "nxs_pat"
    serialized = schema.text.replace(" ", "")
    assert '"type":"null"' not in serialized
    assert '"const":' not in serialized


def test_agent_api_tool_workspace_task_execution_and_artifacts(agent_api: dict[str, Any]) -> None:
    created = _run_tool(
        agent_api,
        "create_workspace",
        parameters={"name": "Model workspace", "description": "Created through a local model tool"},
    )
    assert created["ok"] is True
    workspace_id = created["data"]["id"]

    started = _run_tool(agent_api, "start_workspace", workspace_id=workspace_id)
    assert started["data"]["status"] == "running"

    executed = _run_tool(
        agent_api,
        "execute",
        workspace_id=workspace_id,
        parameters={"code": "print(21 * 2)", "language": "python"},
    )
    assert executed["ok"] is True
    assert executed["data"]["stdout"].strip() == "42"

    task = _run_tool(
        agent_api,
        "create_task",
        workspace_id=workspace_id,
        parameters={"instruction": "Write the result", "priority": "high", "max_retries": 2},
    )
    assert task["ok"] is True

    content = b"tool artifact\n"
    uploaded = _run_tool(
        agent_api,
        "upload_artifact",
        workspace_id=workspace_id,
        parameters={
            "filename": "tool.txt",
            "mime_type": "text/plain",
            "task_id": task["data"]["id"],
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert uploaded["ok"] is True

    downloaded = _run_tool(
        agent_api,
        "download_artifact",
        workspace_id=workspace_id,
        parameters={"artifact_id": uploaded["data"]["id"]},
    )
    assert base64.b64decode(downloaded["data"]["content_base64"]) == content


def test_agent_api_tool_enforces_caller_and_pat_scopes(agent_api: dict[str, Any]) -> None:
    anonymous = _run_tool(agent_api, "list_workspaces", caller=AgentToolCaller(user=None, token=None))
    assert anonymous["ok"] is False
    assert anonymous["error"]["code"] == "AUTHENTICATED_USER_REQUIRED"

    limited = user_store.create_api_key(
        str(agent_api["db_path"]),
        user_id=agent_api["user"].id,
        name="tool-read-only",
        policy={"scopes": ["workspaces:read"]},
    )
    limited_caller = _tool_caller(agent_api, limited["token"])
    denied = _run_tool(
        agent_api,
        "create_workspace",
        parameters={"name": "Must not exist"},
        caller=limited_caller,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert denied["error"]["details"]["required_scope"] == "workspaces:write"

    listing = _run_tool(agent_api, "list_workspaces", caller=limited_caller)
    assert listing["ok"] is True
    assert listing["data"]["items"] == []
