from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-gateway-token")

from app import deployment_admin_routes, deployment_control


def test_configuration_requires_endpoint_and_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPLOY_CONTROL_BASE_URL", raising=False)
    monkeypatch.delenv("DEPLOY_CONTROL_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEPLOY_CONTROL_GATEWAY_TOKEN_FILE", raising=False)
    assert deployment_control.configuration()["configured"] is False

    monkeypatch.setenv("DEPLOY_CONTROL_BASE_URL", "http://copyfail:9220/")
    monkeypatch.setenv("DEPLOY_CONTROL_GATEWAY_TOKEN", "gateway-service-token")
    config = deployment_control.configuration()
    assert config["configured"] is True
    assert config["base_url"] == "http://copyfail:9220"
    assert config["credential_source"] == "environment"


def test_service_token_prefers_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("DEPLOY_CONTROL_GATEWAY_TOKEN", "from-environment")
    monkeypatch.setenv("DEPLOY_CONTROL_GATEWAY_TOKEN_FILE", str(token_file))
    assert deployment_control.service_token() == "from-file"


def test_topology_components_include_optional_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps(
            {
                "hosts": {
                    "ai2": {
                        "components": ["gateway", "cloudflared"],
                        "optional_components": ["mlx"],
                    },
                    "copyfail": {"components": ["deployment-control"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))
    assert deployment_admin_routes._topology_components() == {
        "ai2": ["gateway", "cloudflared", "mlx"],
        "copyfail": ["deployment-control"],
    }


def test_gateway_deployment_expands_cloudflared_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps(
            {"hosts": {"ai2": {"components": ["gateway", "cloudflared"]}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))
    body = deployment_admin_routes.AdminDeploymentRequest(
        host="ai2",
        components=["gateway"],
    )
    expanded = deployment_admin_routes._expand_component_dependencies(body)
    assert expanded.components == ["gateway", "cloudflared"]


def test_gateway_dependency_is_not_added_on_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps(
            {
                "hosts": {
                    "ai2": {"components": ["gateway", "cloudflared"]},
                    "dev": {"components": ["gateway"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))
    body = deployment_admin_routes.AdminDeploymentRequest(host="dev", components=["gateway"])
    assert deployment_admin_routes._expand_component_dependencies(body).components == ["gateway"]


def test_topology_request_rejects_misplaced_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = tmp_path / "production.json"
    topology.write_text(
        json.dumps({"hosts": {"ai2": {"components": ["gateway"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE", str(topology))
    body = deployment_admin_routes.AdminDeploymentRequest(
        host="ai2",
        components=["gateway", "vllm-fast"],
    )
    with pytest.raises(HTTPException) as exc_info:
        deployment_admin_routes._validate_topology_request(body)
    assert exc_info.value.status_code == 400
    assert "vllm-fast" in str(exc_info.value.detail)


def test_controller_auth_failure_is_not_exposed_to_browser() -> None:
    error = deployment_control.DeploymentControlError(
        "invalid deployment-control token",
        status_code=401,
    )
    translated = deployment_admin_routes._translate_error(error)
    assert translated.status_code == 502
    assert "Gateway service credential" in translated.detail["message"]
    assert "invalid deployment-control token" not in translated.detail["message"]


def test_admin_actor_uses_authenticated_identity() -> None:
    class Admin:
        id = 42
        username = "operator"
        email = "operator@example.com"

    assert deployment_admin_routes._admin_actor(Admin()) == "nexus-admin:operator"


def test_routes_are_composed_once() -> None:
    from app.honcho_memory_routes import router

    expected = {
        ("/ui/admin/deployments", "GET"),
        ("/ui/api/admin/deployments/status", "GET"),
        ("/ui/api/admin/deployments", "GET"),
        ("/ui/api/admin/deployments", "POST"),
        ("/ui/api/admin/deployments/{job_id}", "GET"),
    }
    actual: list[tuple[str, str]] = []
    for route in router.routes:
        path = str(getattr(route, "path", ""))
        for method in set(getattr(route, "methods", set()) or set()):
            pair = (path, method)
            if pair in expected:
                actual.append(pair)
    assert set(actual) == expected
    assert len(actual) == len(expected)
