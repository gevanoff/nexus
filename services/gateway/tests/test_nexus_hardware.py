from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import nexus_hardware


def _live_payload() -> dict[str, object]:
    return {
        "generated_at": 1_700_000_000,
        "hosts": [
            {
                "name": "ai2",
                "cpu": {"model_name": "Apple M3 Ultra", "logical_cpus": 32, "physical_cpus": 32},
                "memory": {"total_mb": 524288, "used_mb": 1000, "available_mb": 523000},
                "gpus": [],
                "updated_at": 1_700_000_001,
            },
            {
                "name": "ai1",
                "cpu": {"model_name": "Test Intel Workstation", "logical_cpus": 64},
                "memory": {"total_mb": 131072, "used_mb": 2000, "available_mb": 129000},
                "gpus": [
                    {
                        "index": 0,
                        "name": "NVIDIA Test GPU",
                        "memory_total_mb": 81920,
                        "memory_used_mb": 1024,
                        "memory_free_mb": 80896,
                        "utilization_gpu_pct": 1,
                    }
                ],
                "updated_at": 1_700_000_002,
            },
            {
                "name": "ada2",
                "cpu": {"model_name": "Test Ada Host", "logical_cpus": 32},
                "memory": {"total_mb": 262144, "used_mb": 2000, "available_mb": 260000},
                "gpus": [
                    {
                        "index": 0,
                        "name": "NVIDIA RTX 6000 Ada Generation",
                        "memory_total_mb": 49152,
                        "memory_used_mb": 1024,
                        "memory_free_mb": 48128,
                        "utilization_gpu_pct": 1,
                    }
                ],
                "updated_at": 1_700_000_003,
            },
            {
                "name": "meltdown",
                "cpu": {"model_name": "Test Meltdown Host", "logical_cpus": 12},
                "memory": {"total_mb": 48100, "used_mb": 2000, "available_mb": 46000},
                "gpus": [
                    {
                        "index": 0,
                        "name": "NVIDIA GeForce RTX 5060 Ti",
                        "memory_total_mb": 16311,
                        "memory_used_mb": 1024,
                        "memory_free_mb": 15287,
                        "utilization_gpu_pct": 1,
                    }
                ],
                "updated_at": 1_700_000_004,
            },
            {
                "name": "copyfail",
                "cpu": {"model_name": "Test Copyfail Host", "logical_cpus": 2},
                "memory": {"total_mb": 7600, "used_mb": 200, "available_mb": 7200},
                "gpus": [],
                "updated_at": 1_700_000_006,
            },
            {
                "name": "migraine",
                "error": "ssh unavailable",
                "cpu": {},
                "memory": {},
                "gpus": [],
                "updated_at": 1_700_000_005,
            },
        ],
    }


def test_lifecycle_payload_updates_runtime_and_cached_snapshot(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "hardware_snapshot.json"
    monkeypatch.setattr(nexus_hardware, "_RUNTIME_SNAPSHOT", None)
    monkeypatch.setattr(nexus_hardware.S, "NEXUS_HARDWARE_SNAPSHOT_PATH", str(snapshot_path))

    snapshot = nexus_hardware.update_hardware_snapshot_from_lifecycle_payload(_live_payload())
    prompt_context = nexus_hardware.scheduled_task_hardware_context()

    assert snapshot["source"] == "live_lifecycle_refresh"
    assert snapshot_path.exists()
    cached = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert cached["hosts"]["ai1"]["cpu"] == "Test Intel Workstation"
    assert "source: live_lifecycle_refresh" in prompt_context
    assert "ai1: linux_x86_64; Test Intel Workstation; 64 logical CPUs" in prompt_context
    assert "NVIDIA Test GPU 80 GiB" in prompt_context
    assert "meltdown: linux_x86_64; Test Meltdown Host; 12 logical CPUs" in prompt_context
    assert "NVIDIA GeForce RTX 5060 Ti 15.9 GiB" in prompt_context
    assert "copyfail: linux_x86_64; Test Copyfail Host; 2 logical CPUs" in prompt_context
    assert "Do not schedule model-serving backends here" in prompt_context
    assert "migraine: macos_arm64; Apple M2" in prompt_context
    assert "Client-only Hermes Gateway and Telegram bot host" in prompt_context
    assert "Do not choose migraine for Nexus model placement" in prompt_context
    assert "lifecycle probe failed" in prompt_context


def test_scheduled_context_uses_last_cached_snapshot_when_runtime_is_empty(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "hardware_snapshot.json"
    cached_snapshot = nexus_hardware.update_hardware_snapshot_from_lifecycle_payload(_live_payload(), persist=False)
    snapshot_path.write_text(json.dumps(cached_snapshot), encoding="utf-8")
    monkeypatch.setattr(nexus_hardware, "_RUNTIME_SNAPSHOT", None)
    monkeypatch.setattr(nexus_hardware.S, "NEXUS_HARDWARE_SNAPSHOT_PATH", str(snapshot_path))

    prompt_context = nexus_hardware.scheduled_task_hardware_context()

    assert "source: last_cached_snapshot" in prompt_context
    assert "Test Intel Workstation" in prompt_context
    assert "NVIDIA Test GPU 80 GiB" in prompt_context
    assert "Test Meltdown Host" in prompt_context
    assert "Test Copyfail Host" in prompt_context


@pytest.mark.asyncio
async def test_startup_refresh_falls_back_to_baseline_when_lifecycle_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(nexus_hardware, "_RUNTIME_SNAPSHOT", None)
    monkeypatch.setattr(nexus_hardware.S, "NEXUS_HARDWARE_SNAPSHOT_PATH", str(tmp_path / "missing_snapshot.json"))
    monkeypatch.setattr(nexus_hardware.S, "LIFECYCLE_MANAGER_BASE_URL", "")
    monkeypatch.setattr(nexus_hardware.S, "NEXUS_HARDWARE_REFRESH_ON_STARTUP", True)

    snapshot = await nexus_hardware.refresh_hardware_snapshot_from_lifecycle()

    assert snapshot["source"] == "checked_in_baseline"
    assert snapshot["hosts"]["ai1"]["cpu"] == "12th Gen Intel Core i7-12700F"
    assert snapshot["hosts"]["meltdown"]["cpu"] == "Intel Core i7-5930K"
    assert snapshot["hosts"]["copyfail"]["cpu"] == "Intel Celeron J3355 @ 2.00GHz"
    assert "lifecycle manager URL is not configured" in " ".join(snapshot["warnings"])
