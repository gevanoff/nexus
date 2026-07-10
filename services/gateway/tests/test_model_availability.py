from __future__ import annotations

import os
import json
from dataclasses import dataclass

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import model_availability
from app.model_availability import hf_model_cache_details, hf_model_cache_entries, hf_model_cache_state


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


def test_hf_model_cache_state_rejects_incomplete_sharded_snapshot(tmp_path):
    model = "mlx-community/Sharded-Model"
    snapshot = tmp_path / "hub" / "models--mlx-community--Sharded-Model" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model-00001-of-00003.safetensors").write_text("", encoding="utf-8")
    (snapshot / "model-00003-of-00003.safetensors").write_text("", encoding="utf-8")

    assert hf_model_cache_state(model, str(tmp_path)) == "missing"

    (snapshot / "model-00002-of-00003.safetensors").write_text("", encoding="utf-8")
    assert hf_model_cache_state(model, str(tmp_path)) == "cached"


def test_hf_model_cache_state_does_not_trust_cached_metadata_with_incomplete_files(tmp_path):
    payload = {
        "models": {
            "mlx-community/Partial": {
                "state": "cached",
                "incomplete_count": 2,
                "incomplete_bytes": 1234,
            },
            "mlx-community/IncompleteSnapshot": {
                "state": "cached",
                "complete_snapshot_count": 0,
                "incomplete_snapshot_count": 1,
            },
        }
    }
    (tmp_path / ".nexus_cache_status.json").write_text(json.dumps(payload), encoding="utf-8")

    assert hf_model_cache_state("mlx-community/Partial", str(tmp_path)) == "fetching"
    assert hf_model_cache_state("mlx-community/IncompleteSnapshot", str(tmp_path)) == "missing"


def test_hf_model_cache_details_marks_fetching_active_or_stalled_from_metadata(tmp_path):
    payload = {
        "generated_at": 2000,
        "models": {
            "mlx-community/Active": {
                "state": "fetching",
                "incomplete_count": 2,
                "incomplete_bytes": 1234,
                "newest_incomplete_mtime": 1950,
                "oldest_incomplete_mtime": 1900,
            },
            "mlx-community/Stalled": {
                "state": "fetching",
                "incomplete_count": 1,
                "incomplete_bytes": 5678,
                "newest_incomplete_mtime": 1000,
            },
        },
    }
    (tmp_path / ".nexus_cache_status.json").write_text(json.dumps(payload), encoding="utf-8")

    active = hf_model_cache_details("mlx-community/Active", str(tmp_path), stalled_after_sec=600, now=2000)
    stalled = hf_model_cache_details("mlx-community/Stalled", str(tmp_path), stalled_after_sec=600, now=2000)

    assert active["state"] == "fetching"
    assert active["fetch_activity"]["status"] == "active"
    assert active["fetch_activity"]["incomplete_bytes"] == 1234
    assert stalled["fetch_activity"]["status"] == "stalled"
    assert stalled["fetch_activity"]["last_progress_age_sec"] == 1000


def test_route_with_model_fallback_can_switch_to_fast_backend(monkeypatch):
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_BACKEND", "local_vllm_fast", raising=False)
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_MODEL", "fast-model", raising=False)

    def unavailable(backend, model):
        return "fetching" if backend == "local_mlx" and model == "minimax" else None

    monkeypatch.setattr(model_availability, "model_unavailable_reason", unavailable)

    route = model_availability.route_with_model_fallback(Route("local_mlx", "minimax", "policy:model"))

    assert route.backend == "local_vllm_fast"
    assert route.model == "fast-model"
    assert route.reason == "policy:model->fallback:fetching:local_mlx:minimax"


def test_route_with_model_fallback_keeps_explicit_mlx_alias(monkeypatch):
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_BACKEND", "local_vllm_fast", raising=False)
    monkeypatch.setattr(model_availability.S, "MLX_FALLBACK_MODEL", "fast-model", raising=False)
    monkeypatch.setattr(model_availability, "model_unavailable_reason", lambda _backend, _model: "fetching")

    route = model_availability.route_with_model_fallback(Route("local_mlx", "glm", "alias:model"))

    assert route == Route("local_mlx", "glm", "alias:model")
