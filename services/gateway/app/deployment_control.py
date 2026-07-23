from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


class DeploymentControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.detail = detail


def base_url() -> str:
    return str(os.getenv("DEPLOY_CONTROL_BASE_URL") or "").strip().rstrip("/")


def timeout_seconds() -> float:
    try:
        return max(1.0, min(float(os.getenv("DEPLOY_CONTROL_TIMEOUT_SEC") or 20.0), 120.0))
    except (TypeError, ValueError):
        return 20.0


def _read_token_file(path: str) -> str:
    target = Path(path).expanduser()
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeploymentControlError(
            f"deployment controller credential file is unavailable: {target}",
            detail={"token_file": str(target)},
        ) from exc


def service_token() -> str:
    token_file = str(os.getenv("DEPLOY_CONTROL_GATEWAY_TOKEN_FILE") or "").strip()
    if token_file:
        token = _read_token_file(token_file)
    else:
        token = str(os.getenv("DEPLOY_CONTROL_GATEWAY_TOKEN") or "").strip()
    if not token:
        raise DeploymentControlError(
            "deployment controller Gateway credential is not configured",
            detail={"configured": False},
        )
    return token


def configuration() -> dict[str, Any]:
    endpoint = base_url()
    token_file = str(os.getenv("DEPLOY_CONTROL_GATEWAY_TOKEN_FILE") or "").strip()
    has_inline_token = bool(str(os.getenv("DEPLOY_CONTROL_GATEWAY_TOKEN") or "").strip())
    has_token_file = bool(token_file and Path(token_file).expanduser().is_file())
    return {
        "configured": bool(endpoint and (has_inline_token or has_token_file)),
        "base_url": endpoint,
        "credential_source": "file" if token_file else ("environment" if has_inline_token else "none"),
        "token_file_present": has_token_file if token_file else None,
        "timeout_sec": timeout_seconds(),
    }


async def request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> Any:
    endpoint = base_url()
    if not endpoint:
        raise DeploymentControlError(
            "DEPLOY_CONTROL_BASE_URL is not configured",
            detail={"configured": False},
        )

    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        headers["Authorization"] = f"Bearer {service_token()}"

    url = f"{endpoint}/{str(path or '').lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds()) as client:
            response = await client.request(method.upper(), url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise DeploymentControlError(
            "deployment controller request timed out",
            status_code=504,
            detail={"url": url},
        ) from exc
    except httpx.HTTPError as exc:
        raise DeploymentControlError(
            f"deployment controller is unreachable: {type(exc).__name__}",
            detail={"url": url},
        ) from exc

    try:
        body: Any = response.json()
    except ValueError:
        body = {"detail": response.text[:4000]}

    if response.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        message = str(detail or f"deployment controller returned HTTP {response.status_code}")
        raise DeploymentControlError(
            message,
            status_code=response.status_code,
            detail=body,
        )
    return body
