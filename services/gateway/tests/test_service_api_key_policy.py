from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth, ui_routes, user_store


def _request(token: str, *, path: str = "/v1/telegram/memory/status") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "client": ("127.0.0.1", 12345),
        }
    )


def _service_key(tmp_path, monkeypatch, *, policy: dict) -> str:
    db_path = str(tmp_path / "users.sqlite")
    user_store.init_db(db_path)
    user = user_store.create_user(db_path, username="cinder-service", password="unused-secret")
    created = user_store.create_api_key(
        db_path,
        user_id=user.id,
        name="Cinder Telegram bridge",
        policy=policy,
    )
    monkeypatch.setattr(auth.S, "USER_DB_PATH", db_path)
    monkeypatch.setattr(auth.S, "USER_AUTH_ENABLED", True)
    monkeypatch.setattr(auth.S, "GATEWAY_BEARER_TOKEN", "primary-test-token")
    monkeypatch.setattr(auth.S, "GATEWAY_BEARER_TOKENS", "")
    monkeypatch.setattr(auth.S, "GATEWAY_TOKEN_POLICIES_JSON", "")
    auth._parse_token_policies.cache_clear()
    return str(created["token"])


def test_scoped_api_key_can_access_telegram_service_routes(tmp_path, monkeypatch) -> None:
    token = _service_key(
        tmp_path,
        monkeypatch,
        policy={"service_access": ["telegram_bridge"], "model_allowlist": ["cinder-chat"]},
    )
    user = ui_routes._require_static_bearer_service(_request(token))
    assert user.username == "cinder-service"


def test_unscoped_api_key_cannot_access_telegram_service_routes(tmp_path, monkeypatch) -> None:
    token = _service_key(tmp_path, monkeypatch, policy={"model_allowlist": ["cinder-chat"]})
    with pytest.raises(HTTPException) as exc:
        ui_routes._require_static_bearer_service(_request(token))
    assert exc.value.status_code == 403


def test_model_allowlist_rejects_other_models() -> None:
    req = SimpleNamespace(
        state=SimpleNamespace(token_policy={"model_allowlist": ["cinder-chat"]})
    )
    auth.enforce_token_model_allowlist(req, "cinder-chat")
    with pytest.raises(HTTPException) as exc:
        auth.enforce_token_model_allowlist(req, "coder")
    assert exc.value.status_code == 403


def test_path_allowlist_rejects_other_endpoints(tmp_path, monkeypatch) -> None:
    token = _service_key(
        tmp_path,
        monkeypatch,
        policy={"path_allowlist": ["/v1/models", "/v1/telegram/*"]},
    )
    auth.require_bearer(_request(token, path="/v1/models"))
    auth.require_bearer(_request(token, path="/v1/telegram/memory/status"))
    with pytest.raises(HTTPException) as exc:
        auth.require_bearer(_request(token, path="/v1/embeddings"))
    assert exc.value.status_code == 403
