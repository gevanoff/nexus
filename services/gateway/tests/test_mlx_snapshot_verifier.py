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


def test_native_mlx_installer_ships_prefetch_verifier_next_to_helper():
    root = Path(__file__).resolve().parents[3]
    installer = (root / "services" / "mlx" / "scripts" / "install-native-macos.sh").read_text(encoding="utf-8")

    assert 'MLX_SNAPSHOT_VERIFIER="${MLX_VENV}/bin/verify_model_snapshot.py"' in installer
    assert 'verify_model_snapshot.py" "${MLX_SNAPSHOT_VERIFIER}' in installer


def test_native_mlx_installer_persists_batching_mode_and_honors_readiness_timeout():
    root = Path(__file__).resolve().parents[3]
    installer = (root / "services" / "mlx" / "scripts" / "install-native-macos.sh").read_text(encoding="utf-8")
    launcher = (root / "services" / "mlx" / "scripts" / "run-native-macos.sh").read_text(encoding="utf-8")

    assert "--disable-batching" in installer
    assert 'update_env_file_key "${MLX_ENV_FILE}" MLX_DISABLE_BATCHING' in installer
    assert "SECONDS + MLX_MODEL_READY_TIMEOUT_SEC" in installer
    assert 'batching_args+=(--disable-batching)' in launcher


def test_native_mlx_launcher_supports_constrained_official_server_mode():
    root = Path(__file__).resolve().parents[3]
    installer = (root / "services" / "mlx" / "scripts" / "install-native-macos.sh").read_text(encoding="utf-8")
    launcher = (root / "services" / "mlx" / "scripts" / "run-native-macos.sh").read_text(encoding="utf-8")

    assert "--server-impl" in installer
    assert 'update_env_file_key "${MLX_ENV_FILE}" MLX_SERVER_IMPL' in installer
    assert '-m mlx_lm server' in launcher
    assert '--decode-concurrency "$MLX_DECODE_CONCURRENCY"' in launcher
    assert '--prompt-cache-size "$MLX_PROMPT_CACHE_SIZE"' in launcher
