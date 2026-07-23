from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app import deployment_control
from app.ui_routes import _require_admin, _require_ui_access


router = APIRouter()
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEFAULT_TOPOLOGY_FILE = Path("/workspace/nexus/deploy/topology/production.json")


class AdminDeploymentRequest(BaseModel):
    host: str = Field(min_length=1, max_length=100)
    components: list[str] = Field(min_length=1, max_length=16)
    branch: str = Field(default="main", min_length=1, max_length=200)
    environment: str = Field(default="prod", pattern=r"^prod$")
    reason: str = Field(default="", max_length=500)

    @field_validator("host", "branch", "reason", mode="before")
    @classmethod
    def _strip_string(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("components")
    @classmethod
    def _normalize_components(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = str(raw or "").strip()
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("at least one component is required")
        return normalized


def _admin(req: Request) -> Any:
    _require_ui_access(req)
    return _require_admin(req)


def _admin_actor(admin: Any) -> str:
    username = str(getattr(admin, "username", "") or "").strip()
    email = str(getattr(admin, "email", "") or "").strip()
    identifier = username or email or str(getattr(admin, "id", "unknown"))
    return f"nexus-admin:{identifier}"[:100]


def _topology_file() -> Path:
    configured = str(os.getenv("NEXUS_DEPLOYMENT_TOPOLOGY_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if _DEFAULT_TOPOLOGY_FILE.is_file():
        return _DEFAULT_TOPOLOGY_FILE
    return Path(__file__).resolve().parents[3] / "deploy" / "topology" / "production.json"


def _topology_components() -> dict[str, list[str]]:
    path = _topology_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, dict):
        return {}
    result: dict[str, list[str]] = {}
    for host, raw in hosts.items():
        if not isinstance(raw, dict):
            continue
        components = raw.get("components") if isinstance(raw.get("components"), list) else []
        optional = raw.get("optional_components") if isinstance(raw.get("optional_components"), list) else []
        assigned: list[str] = []
        for item in [*components, *optional]:
            value = str(item or "").strip()
            if value and value not in assigned:
                assigned.append(value)
        result[str(host)] = assigned
    return result


def _enrich_capabilities(capabilities: Any) -> Any:
    if not isinstance(capabilities, dict):
        return capabilities
    return {
        **capabilities,
        "topology_components": _topology_components(),
        "topology_file": str(_topology_file()),
    }


def _validate_topology_request(body: AdminDeploymentRequest) -> None:
    mapping = _topology_components()
    if not mapping:
        return
    assigned = mapping.get(body.host)
    if assigned is None:
        raise HTTPException(status_code=400, detail=f"host is not present in deployment topology: {body.host}")
    misplaced = [component for component in body.components if component not in assigned]
    if misplaced:
        raise HTTPException(
            status_code=400,
            detail=(
                f"component is not assigned to {body.host} in production topology: "
                f"{', '.join(misplaced)}"
            ),
        )


def _translate_error(exc: deployment_control.DeploymentControlError) -> HTTPException:
    upstream_status = int(exc.status_code or 503)
    if upstream_status in {400, 404, 409, 422}:
        status = upstream_status
        message = str(exc)
    elif upstream_status in {401, 403}:
        status = 502
        message = "deployment controller rejected the Gateway service credential"
    elif upstream_status == 504:
        status = 504
        message = str(exc)
    else:
        status = 503
        message = str(exc)
    return HTTPException(
        status_code=status,
        detail={
            "error": "deployment_control_unavailable" if status >= 500 else "deployment_request_rejected",
            "message": message,
            "upstream_status": upstream_status,
        },
    )


async def _controller_call(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> Any:
    try:
        return await deployment_control.request_json(
            method,
            path,
            payload=payload,
            authenticated=authenticated,
        )
    except deployment_control.DeploymentControlError as exc:
        raise _translate_error(exc) from exc


@router.get("/ui/admin/deployments", include_in_schema=False)
async def deployment_admin_page(req: Request) -> FileResponse:
    _admin(req)
    return FileResponse(_STATIC_DIR / "admin_deployments.html")


@router.get("/ui/api/admin/deployments/status", include_in_schema=False)
async def deployment_admin_status(req: Request, limit: int = 20) -> dict[str, Any]:
    _admin(req)
    config = deployment_control.configuration()
    if not config["configured"]:
        return {
            "configured": False,
            "controller_reachable": False,
            "configuration": config,
            "capabilities": None,
            "deployments": [],
            "error": "Deployment Control is not configured for the Gateway.",
        }

    bounded = max(1, min(int(limit), 100))
    health: Any = None
    capabilities: Any = None
    deployments: Any = []
    errors: list[str] = []

    try:
        health = await deployment_control.request_json("GET", "/health", authenticated=False)
    except deployment_control.DeploymentControlError as exc:
        errors.append(str(exc))

    try:
        capabilities = _enrich_capabilities(
            await deployment_control.request_json("GET", "/v1/capabilities")
        )
    except deployment_control.DeploymentControlError as exc:
        errors.append(str(exc))

    try:
        deployments = await deployment_control.request_json(
            "GET", f"/v1/deployments?limit={bounded}"
        )
    except deployment_control.DeploymentControlError as exc:
        errors.append(str(exc))

    return {
        "configured": True,
        "controller_reachable": bool(
            isinstance(health, dict) and str(health.get("status") or "").lower() == "ok"
        ),
        "configuration": config,
        "health": health,
        "capabilities": capabilities,
        "deployments": deployments if isinstance(deployments, list) else [],
        "errors": errors,
    }


@router.get("/ui/api/admin/deployments", include_in_schema=False)
async def deployment_admin_list(req: Request, limit: int = 20) -> Any:
    _admin(req)
    bounded = max(1, min(int(limit), 100))
    return await _controller_call("GET", f"/v1/deployments?limit={bounded}")


@router.get("/ui/api/admin/deployments/{job_id}", include_in_schema=False)
async def deployment_admin_get(req: Request, job_id: str) -> Any:
    _admin(req)
    normalized = str(job_id or "").strip()
    if not normalized or len(normalized) > 100:
        raise HTTPException(status_code=400, detail="invalid deployment job id")
    return await _controller_call("GET", f"/v1/deployments/{normalized}")


@router.post("/ui/api/admin/deployments", include_in_schema=False)
async def deployment_admin_create(
    req: Request,
    body: AdminDeploymentRequest,
) -> JSONResponse:
    admin = _admin(req)
    _validate_topology_request(body)
    payload = body.model_dump()
    payload["requested_by"] = _admin_actor(admin)
    result = await _controller_call("POST", "/v1/deployments", payload=payload)
    return JSONResponse(status_code=202, content=result)
