from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_routes


def test_coding_model_for_user_normalizes_stale_preference(monkeypatch):
    monkeypatch.setattr(
        coding_routes,
        "_coding_settings_for_user",
        lambda _user: {"model_preference": "mlx-community/GLM-5-4bit"},
    )
    monkeypatch.setattr(
        coding_routes.coding_model_policy,
        "normalize_preferred_coding_model",
        lambda value: "coder" if value == "mlx-community/GLM-5-4bit" else value,
    )

    assert coding_routes._coding_model_for_user(user=object(), requested=None) == "coder"


def test_coding_model_for_user_keeps_explicit_request(monkeypatch):
    monkeypatch.setattr(
        coding_routes,
        "_coding_settings_for_user",
        lambda _user: {"model_preference": "coder"},
    )

    assert coding_routes._coding_model_for_user(user=object(), requested="reasoning") == "reasoning"
