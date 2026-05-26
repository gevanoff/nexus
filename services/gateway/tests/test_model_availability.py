from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.model_availability import hf_model_cache_state


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
