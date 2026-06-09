from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_lifecycle_main():
    root = Path(__file__).resolve().parents[3]
    module_path = root / "services" / "lifecycle-manager" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("nexus_lifecycle_main_for_tests", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_docker_probe_handles_external_colima_socket() -> None:
    module = _load_lifecycle_main()
    command = module.LifecycleManager._docker_probe_command()

    assert "ps --format" in command
    assert "/ai-data/var/lib/colima/default/docker.sock" in command
    assert "/Volumes/ai_data/var/lib/colima/default/docker.sock" in command
    assert "DOCKER_HOST=\"unix://$sock\"" in command


def test_component_container_match_allows_compose_suffixes() -> None:
    module = _load_lifecycle_main()

    assert module.LifecycleManager._component_container_active("nexus-gateway", "nexus-gateway")
    assert module.LifecycleManager._component_container_active("nexus-gateway-1", "nexus-gateway")
    assert not module.LifecycleManager._component_container_active(
        "nexus-gateway-registrar-1",
        "nexus-gateway",
    )
