from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import main


def test_validate_request_accepts_component_scoped_main_deploy(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_CONTROL_ENFORCE_TOPOLOGY", "false")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_HOSTS", "ai2,ada2")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_COMPONENTS", "gateway,images")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_BRANCHES", "main")
    request = main.DeploymentRequest(host="ada2", components=["images", "images"])

    main._validate_request(request)

    assert request.components == ["images"]


@pytest.mark.parametrize(
    ("host", "components", "branch"),
    [
        ("unknown", ["images"], "main"),
        ("ada2", ["all"], "main"),
        ("ada2", ["images"], "feature/test"),
    ],
)
def test_validate_request_rejects_non_allowlisted_values(
    monkeypatch, host: str, components: list[str], branch: str
) -> None:
    monkeypatch.setenv("DEPLOY_CONTROL_ENFORCE_TOPOLOGY", "false")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_HOSTS", "ada2")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_COMPONENTS", "images")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_BRANCHES", "main")
    request = main.DeploymentRequest(host=host, components=components, branch=branch)

    with pytest.raises(HTTPException) as exc_info:
        main._validate_request(request)

    assert exc_info.value.status_code == 400


def test_deployment_command_is_argument_safe(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "deploy" / "scripts" / "remote-deploy.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DEPLOY_CONTROL_REPO_ROOT", str(tmp_path))
    job = main.DeploymentJob(
        id="job",
        status="queued",
        host="ada2",
        components=["images", "invokeai"],
        environment="prod",
        branch="main",
        reason="test",
        requested_by="pytest",
        created_at=1,
    )

    command = main._deployment_command(job)

    assert command == [
        str(script),
        "--yes",
        "--components",
        "images,invokeai",
        "--topology-host",
        "ada2",
        "prod",
        "main",
    ]


def test_validate_request_enforces_topology_placement(monkeypatch, tmp_path: Path) -> None:
    topology = tmp_path / "deploy" / "topology" / "production.json"
    topology.parent.mkdir(parents=True)
    topology.write_text(
        '{"hosts":{"ada2":{"components":["images","invokeai"]}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DEPLOY_CONTROL_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_HOSTS", "ada2")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_COMPONENTS", "gateway,images")
    monkeypatch.setenv("DEPLOY_CONTROL_ALLOWED_BRANCHES", "main")
    monkeypatch.setenv("DEPLOY_CONTROL_ENFORCE_TOPOLOGY", "true")

    main._validate_request(main.DeploymentRequest(host="ada2", components=["images"]))
    with pytest.raises(HTTPException) as exc_info:
        main._validate_request(
            main.DeploymentRequest(host="ada2", components=["gateway"])
        )

    assert exc_info.value.status_code == 400


def test_auth_requires_matching_bearer_token(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("expected-token\n", encoding="utf-8")
    monkeypatch.setenv("DEPLOY_CONTROL_TOKEN_FILE", str(token_file))

    asyncio.run(main.require_auth("Bearer expected-token"))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.require_auth("Bearer wrong-token"))

    assert exc_info.value.status_code == 401


def test_log_redaction_hides_credentials() -> None:
    assert main._redact_log_line("token=super-secret") == "token=[redacted]"
    assert main._redact_log_line("password: hunter2") == "password: [redacted]"
