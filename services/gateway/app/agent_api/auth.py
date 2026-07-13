from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from app.auth import require_bearer
from app.agent_api.errors import ApiError


DEFAULT_AGENT_SCOPES = (
    "workspaces:read",
    "workspaces:write",
    "tasks:read",
    "tasks:write",
    "execute",
    "artifacts:read",
    "artifacts:write",
)


@dataclass(frozen=True)
class AgentAuthContext:
    user: Any
    token: dict[str, Any]
    scopes: tuple[str, ...]
    workspace_id: str | None
    rate_limits: dict[str, Any]

    @property
    def user_id(self) -> int:
        return int(self.user.id)


@dataclass(frozen=True)
class AgentToolCaller:
    """Authenticated caller state passed internally to model tool execution."""

    user: Any | None
    token: dict[str, Any] | None
    allow_session_user: bool = False


def _policy_scopes(policy: dict[str, Any]) -> tuple[str, ...]:
    raw = policy.get("scopes")
    if raw is None:
        return DEFAULT_AGENT_SCOPES
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    else:
        values = []
    return tuple(dict.fromkeys(item for item in values if item))


def _context_for_identity(
    *,
    user: Any,
    token: dict[str, Any],
    required_scope: str | None,
) -> AgentAuthContext:
    policy = token.get("policy") if isinstance(token.get("policy"), dict) else {}
    scopes = _policy_scopes(policy)
    if required_scope and required_scope not in scopes and "*" not in scopes:
        raise ApiError(
            403,
            "INSUFFICIENT_SCOPE",
            f"Token does not grant the required scope: {required_scope}",
            details={"required_scope": required_scope, "granted_scopes": list(scopes)},
        )

    workspace_id = str(policy.get("workspace_id") or "").strip() or None
    rate_limits = policy.get("rate_limits") if isinstance(policy.get("rate_limits"), dict) else {}
    return AgentAuthContext(
        user=user,
        token=token,
        scopes=scopes,
        workspace_id=workspace_id,
        rate_limits=rate_limits,
    )


def agent_tool_caller_from_request(request: Request, *, allow_session_user: bool = False) -> AgentToolCaller:
    state = getattr(request, "state", None)
    user = getattr(state, "user", None) if state is not None else None
    token = getattr(state, "api_key", None) if state is not None else None
    return AgentToolCaller(
        user=user,
        token=token if isinstance(token, dict) else None,
        allow_session_user=bool(allow_session_user),
    )


def authorize_agent_tool(caller: AgentToolCaller | None, required_scope: str | None = None) -> AgentAuthContext:
    if caller is None or caller.user is None:
        raise ApiError(
            403,
            "AUTHENTICATED_USER_REQUIRED",
            "The Nexus Agent API tool requires an authenticated user",
        )
    if isinstance(caller.token, dict):
        return _context_for_identity(user=caller.user, token=caller.token, required_scope=required_scope)
    if not caller.allow_session_user:
        raise ApiError(
            403,
            "PERSONAL_TOKEN_REQUIRED",
            "A personal Nexus API token is required for the Nexus Agent API tool",
        )
    return _context_for_identity(
        user=caller.user,
        token={"id": "ui-session", "policy": {"scopes": list(DEFAULT_AGENT_SCOPES)}},
        required_scope=required_scope,
    )


def validate_api_request(request: Request, required_scope: str | None = None) -> AgentAuthContext:
    try:
        require_bearer(request)
    except HTTPException as exc:
        status = 401 if int(exc.status_code) == 401 else 403
        code = "AUTHENTICATION_REQUIRED" if status == 401 else "INVALID_TOKEN"
        raise ApiError(status, code, str(exc.detail or "Authentication failed")) from exc

    if str(getattr(request.state, "auth_kind", "") or "") != "api_key":
        raise ApiError(
            403,
            "PERSONAL_TOKEN_REQUIRED",
            "A personal Nexus API token is required for the Agent API",
        )

    user = getattr(request.state, "user", None)
    token = getattr(request.state, "api_key", None)
    if user is None or not isinstance(token, dict):
        raise ApiError(403, "INVALID_TOKEN", "The supplied API token is not valid")

    return _context_for_identity(user=user, token=token, required_scope=required_scope)
