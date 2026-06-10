from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_model_policy
from app.model_aliases import ModelAlias


def _configure_huge_lane(monkeypatch, tmp_path):
    state_path = tmp_path / "mlx_huge_lane.json"
    monkeypatch.setattr(coding_model_policy.mlx_huge_lane.S, "MLX_HUGE_LANE_STATE_PATH", str(state_path), raising=False)
    monkeypatch.setattr(coding_model_policy.mlx_huge_lane.S, "MLX_HUGE_MODELS", "model-a,model-b", raising=False)
    monkeypatch.setattr(coding_model_policy.mlx_huge_lane.S, "MLX_HUGE_LANE_DEFAULT_MODEL", "model-a", raising=False)
    monkeypatch.setattr(coding_model_policy.mlx_huge_lane.S, "MLX_HUGE_LANE_ENABLED", True, raising=False)
    coding_model_policy.mlx_huge_lane.mark_ready("model-a")
    return state_path


def test_coder_tracks_active_huge_model(monkeypatch, tmp_path):
    _configure_huge_lane(monkeypatch, tmp_path)

    policy = coding_model_policy.describe_workspace_model("coder")

    assert policy["tracks_coder"] is True
    assert policy["resolved_model"] == "model-a"
    assert policy["run_policy"] == "immediate"


def test_inactive_pinned_huge_model_is_idle_only(monkeypatch, tmp_path):
    _configure_huge_lane(monkeypatch, tmp_path)

    policy = coding_model_policy.describe_workspace_model("model-b")

    assert policy["tracks_coder"] is False
    assert policy["huge_model"] == "model-b"
    assert policy["active_huge_model"] == "model-a"
    assert policy["run_policy"] == "idle_only"
    assert policy["recommended_model"] == "coder"
    assert "only run during idle periods" in policy["warning"]


def test_options_payload_includes_track_coder_and_huge_choices(monkeypatch, tmp_path):
    _configure_huge_lane(monkeypatch, tmp_path)

    payload = coding_model_policy.options_payload()
    options = {item["value"]: item for item in payload["options"]}

    assert payload["current_coder_model"] == "model-a"
    assert options["coder"]["run_policy"] == "immediate"
    assert options["model-a"]["run_policy"] == "immediate"
    assert options["model-b"]["run_policy"] == "idle_only"


def test_options_payload_includes_tool_capable_non_huge_aliases(monkeypatch, tmp_path):
    _configure_huge_lane(monkeypatch, tmp_path)
    monkeypatch.setattr(
        coding_model_policy,
        "get_aliases",
        lambda: {
            "coder": ModelAlias(backend="local_mlx", upstream_model="model-a", tools=True),
            "default": ModelAlias(backend="local_vllm", upstream_model="qwen-default", tools=True),
            "reasoning": ModelAlias(backend="local_vllm", upstream_model="qwen-reasoning", tools=True),
            "fast": ModelAlias(backend="local_vllm_fast", upstream_model="qwen-fast", tools=False),
            "mlx": ModelAlias(backend="local_mlx", upstream_model="model-b", tools=True),
            "embeddings": ModelAlias(backend="local_vllm_embeddings", upstream_model="embedder", tools=False),
        },
    )

    payload = coding_model_policy.options_payload()
    options = {item["value"]: item for item in payload["options"]}

    assert options["default"]["kind"] == "alias"
    assert options["default"]["backend"] == "local_vllm"
    assert options["default"]["model"] == "qwen-default"
    assert options["reasoning"]["kind"] == "alias"
    assert "fast" not in options
    assert "mlx" not in options
    assert "embeddings" not in options


def test_default_alias_is_not_coder_tracking_policy(monkeypatch, tmp_path):
    _configure_huge_lane(monkeypatch, tmp_path)
    monkeypatch.setattr(
        coding_model_policy,
        "get_aliases",
        lambda: {
            "default": ModelAlias(backend="local_vllm", upstream_model="qwen-default", tools=True),
        },
    )

    policy = coding_model_policy.describe_workspace_model("default")

    assert policy["tracks_coder"] is False
    assert policy["status"] == "alias"
    assert policy["resolved_model"] == "qwen-default"
    assert policy["backend"] == "local_vllm"
