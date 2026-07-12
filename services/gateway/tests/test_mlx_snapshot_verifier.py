from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_verifier():
    root = Path(__file__).resolve().parents[3]
    spec = spec_from_file_location("verify_model_snapshot", root / "services" / "mlx" / "scripts" / "verify_model_snapshot.py")
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_verifier_detects_shards_missing_without_incomplete_marker(tmp_path):
    verifier = load_verifier()
    for index in (1, 2, 4):
        (tmp_path / f"model-{index:05d}-of-00004.safetensors").write_bytes(b"")
    errors = verifier.verify_snapshot(tmp_path)
    assert errors == ["model is missing 1 of 4 shards: 3"]


def test_snapshot_verifier_accepts_complete_shards(tmp_path):
    verifier = load_verifier()
    for index in range(1, 4):
        (tmp_path / f"model-{index:05d}-of-00003.safetensors").write_bytes(b"")
    assert verifier.verify_snapshot(tmp_path) == []


def test_snapshot_verifier_checks_nested_shard_groups(tmp_path):
    verifier = load_verifier()
    weights = tmp_path / "weights" / "language"
    weights.mkdir(parents=True)
    (weights / "model-00001-of-00003.safetensors").write_bytes(b"")
    (weights / "model-00003-of-00003.safetensors").write_bytes(b"")

    assert verifier.verify_snapshot(tmp_path) == ["weights/language/model is missing 1 of 3 shards: 2"]
