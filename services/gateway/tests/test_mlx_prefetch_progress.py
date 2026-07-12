from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import ModuleType


def load_prefetch_module():
    root = Path(__file__).resolve().parents[3]
    scripts = root / "services" / "mlx" / "scripts"
    sys.path.insert(0, str(scripts))
    stubbed_hub = "huggingface_hub" not in sys.modules
    if stubbed_hub:
        hub = ModuleType("huggingface_hub")
        hub.HfApi = object
        hub.snapshot_download = lambda **_kwargs: ""
        sys.modules["huggingface_hub"] = hub
    try:
        spec = spec_from_file_location("mlx_prefetch_models", scripts / "prefetch_models.py")
        module = module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))
        if stubbed_hub:
            sys.modules.pop("huggingface_hub", None)


def test_shard_progress_counts_completed_shards_and_partial_bytes(tmp_path):
    prefetch = load_prefetch_module()
    snapshot = tmp_path / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    for index in (1, 2):
        (snapshot / f"model-{index:05d}-of-00004.safetensors").write_bytes(b"complete")
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "third.incomplete").write_bytes(b"partial")

    downloaded, expected, incomplete_bytes = prefetch._shard_progress(
        tmp_path,
        "revision",
        [f"model-{index:05d}-of-00004.safetensors" for index in range(1, 5)],
    )

    assert downloaded == 2
    assert expected == 4
    assert incomplete_bytes == 7


def test_retry_delay_is_exponential_and_bounded():
    prefetch = load_prefetch_module()

    assert prefetch._retry_delay(1, 30, 300) == 30
    assert prefetch._retry_delay(2, 30, 300) == 60
    assert prefetch._retry_delay(6, 30, 300) == 300


def test_shard_progress_does_not_count_an_old_revision(tmp_path):
    prefetch = load_prefetch_module()
    old_snapshot = tmp_path / "snapshots" / "old-revision"
    old_snapshot.mkdir(parents=True)
    shard = "model-00001-of-00002.safetensors"
    (old_snapshot / shard).write_bytes(b"old")

    downloaded, expected, _incomplete_bytes = prefetch._shard_progress(
        tmp_path,
        "new-revision",
        [shard, "model-00002-of-00002.safetensors"],
    )

    assert downloaded == 0
    assert expected == 2


def test_download_status_writes_atomic_shard_progress(tmp_path):
    prefetch = load_prefetch_module()
    snapshot = tmp_path / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    shard_names = [f"model-{index:05d}-of-00003.safetensors" for index in range(1, 4)]
    (snapshot / shard_names[0]).write_bytes(b"complete")

    status = prefetch.DownloadStatus(
        model="mlx-community/Test",
        repo_path=tmp_path,
        revision="revision",
        expected_shards=shard_names,
        max_attempts=5,
    )
    status.refresh_progress()

    payload = json.loads((tmp_path / prefetch.STATUS_FILE).read_text(encoding="utf-8"))
    assert payload["model"] == "mlx-community/Test"
    assert payload["downloaded_shards"] == 1
    assert payload["expected_shards"] == 3
    assert not list(tmp_path.glob(".*.tmp"))


def test_prefetch_retries_and_resumes_before_marking_complete(tmp_path, monkeypatch):
    prefetch = load_prefetch_module()
    model = "mlx-community/Test"
    repo = tmp_path / "models--mlx-community--Test"
    snapshot = repo / "snapshots" / "revision"
    shard_names = [f"model-{index:05d}-of-00002.safetensors" for index in range(1, 3)]
    calls = []

    monkeypatch.setattr(prefetch, "_remote_shards", lambda _model, _token: ("revision", shard_names))
    monkeypatch.setattr(prefetch.time, "sleep", lambda _seconds: None)

    def fake_snapshot_download(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ConnectionError("temporary failure")
        snapshot.mkdir(parents=True, exist_ok=True)
        for shard in shard_names:
            (snapshot / shard).write_bytes(b"complete")
        return str(snapshot)

    monkeypatch.setattr(prefetch, "snapshot_download", fake_snapshot_download)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prefetch_models.py",
            "--model",
            model,
            "--cache-dir",
            str(tmp_path),
            "--max-attempts",
            "3",
            "--retry-base-sec",
            "1",
            "--progress-interval-sec",
            "60",
        ],
    )

    assert prefetch.main() == 0
    assert len(calls) == 2
    payload = json.loads((repo / prefetch.STATUS_FILE).read_text(encoding="utf-8"))
    assert payload["state"] == "complete"
    assert payload["attempt"] == 2
    assert payload["retry_count"] == 1
    assert payload["downloaded_shards"] == 2
    assert not (repo / prefetch.LOCK_FILE).exists()
