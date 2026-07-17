from __future__ import annotations

import socket

from app import browser_urls, ui_routes


def test_browser_accessible_url_resolves_nexus_http_alias(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *, family, type):
        assert host == "ada2"
        assert port == 9090
        assert family == socket.AF_INET
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.42", port))]

    monkeypatch.setattr(browser_urls.socket, "getaddrinfo", fake_getaddrinfo)
    assert (
        browser_urls.browser_accessible_url("http://ada2:9090/models?tab=2#main")
        == "http://192.0.2.42:9090/models?tab=2#main"
    )


def test_browser_accessible_url_preserves_https_and_public_hosts(monkeypatch) -> None:
    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("URL should not be resolved")

    monkeypatch.setattr(browser_urls.socket, "getaddrinfo", unexpected_resolution)
    assert browser_urls.browser_accessible_url("https://ada2:9090/") == "https://ada2:9090/"
    assert browser_urls.browser_accessible_url("http://example.com:9090/") == "http://example.com:9090/"


def test_browser_accessible_url_falls_back_when_alias_does_not_resolve(monkeypatch) -> None:
    def failed_resolution(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(browser_urls.socket, "getaddrinfo", failed_resolution)
    assert browser_urls.browser_accessible_url("http://ada2:9090") == "http://ada2:9090"


def test_invokeai_ui_url_uses_browser_accessible_url(monkeypatch) -> None:
    monkeypatch.setattr(ui_routes.S, "INVOKEAI_UI_URL", "http://ada2:9090/")
    monkeypatch.setattr(
        ui_routes,
        "browser_accessible_url",
        lambda value: value.replace("ada2", "192.0.2.42"),
    )
    assert ui_routes._invokeai_ui_url() == "http://192.0.2.42:9090"
