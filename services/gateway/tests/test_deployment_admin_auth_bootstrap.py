from __future__ import annotations

from pathlib import Path

import pytest

from app import deployment_admin_routes


STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.mark.asyncio
async def test_deployment_admin_shell_allows_auth_client_to_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        deployment_admin_routes,
        "_require_ui_access",
        lambda _req: calls.append("ui_access"),
    )
    monkeypatch.setattr(
        deployment_admin_routes,
        "_require_admin",
        lambda _req: calls.append("admin") or (_ for _ in ()).throw(AssertionError("shell must not require an existing session")),
    )

    response = await deployment_admin_routes.deployment_admin_page(object())

    assert Path(response.path).name == "admin_deployments.html"
    assert calls == ["ui_access"]


def test_deployment_admin_loads_shared_auth_before_page_logic() -> None:
    html = (STATIC_ROOT / "admin_deployments.html").read_text(encoding="utf-8")

    assert '/static/auth_client.js?v=3' in html
    assert '/static/admin_deployments.js?v=4' in html
    assert html.index("auth_client.js") < html.index("admin_deployments.js")


def test_deployment_admin_apis_remain_admin_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_ui_access(_req):
        calls.append("ui_access")

    def fake_admin(_req):
        calls.append("admin")
        return object()

    monkeypatch.setattr(deployment_admin_routes, "_require_ui_access", fake_ui_access)
    monkeypatch.setattr(deployment_admin_routes, "_require_admin", fake_admin)

    deployment_admin_routes._admin(object())

    assert calls == ["ui_access", "admin"]
