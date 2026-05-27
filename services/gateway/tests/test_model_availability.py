from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import model_availability
from app.model_availability import hf_model_cache_entries, hf_model_cache_state


@dataclass(frozen=True)
class Route:
    backend: str
    model: str
    reason: str


def test_hf_model_cache_state_detects_hub_and_direct_layouts(tmp_path):
    model = "mlx-community/Test-Model"

    direct = tmp_path / "models--mlx-community--Test-Model"
    (direct / "snapshots" / "abc123").mkdir(parents=True)
    (direct / "snapshots" / "abc123" / "config.json").write_text("{}", encoding="utf-8")
    assert hf_model_cache_state(model, str(tmp_path)) == "cached"

    (direct / "blobs").mkdir(parents=True, exist_ok=True)
    (direct / "blobs" / "weights.incomplete").write_text("", encoding="utf-8")
    assert hf_model_cache_state(model, str(tmp_path)) == "fetching"

    (direct / "blobs" / "weights.incomplete").unlink()
    direct.rename(tmp_path / "direct-hidden")
    hub = tmp_path / "hub" / "models--mlx-community--Test-Model"
    (hub / "snapshots" / "def456").mkdir(parents=True)
    (hub / "snapshots" / "def456" / "config.json").write_text("{}", encoding="utf-8")
    assert hf_model_cache_state(model, str(tmp_path)) == "cached"


def test_hf_model_cache_entries_lists_repo_states(tmp_path):
    cached = tmp_path / "hub" / "models--mlx-community--Cached-Model"
    (cached / "snapshots" / "abc123").mkdir(parents=True)
    (cached / "snapshots" / "abc123" / "config.json").write_text("{}", encoding="utf-8")

    fetching = tmp_path / "models--mlx-community--Fetching-Model"
    (fetching / "snapshots" / "def456").mkdir(parents=True)
    (fetching / "snapshots" / "def456" / "config.json").write_text("{}", encoding="utf-8")
    (fetching / "blobs").mkdir(parents=True)
    (fetching / "blobs" / "weights.incomplete").write_text("", encoding="utf-8")

    assert hf_model_cache_entries(str(tmp_path)) == {
        "mlx-community/Cached-Model": "cached",
        "mlx-community/Fetching-Model": "fetching",
    }


def test_route_with_model_fallback_can_switch_to_fast_backend(monkeypatch):
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_BACKEND", "local_vllm_fast", raising=False)
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_MODEL", "fast-model", raising=False)

    def unavailable(backend, model):
        return "fetching" if backend == "local_mlx" and model == "minimax" else None

    monkeypatch.setattr(model_availability, "model_unavailable_reason", unavailable)

    route = model_availability.route_with_model_fallback(Route("local_mlx", "minimax", "alias:model"))

    assert route.backend == "local_vllm_fast"
    assert route.model == "fast-model"
    assert route.reason == "alias:model->fallback:fetching:local_mlx:minimax"
