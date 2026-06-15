from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


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
    assert "preferred_output" in command
    assert "fallback_output" in command
    assert "default_output" in command
    assert "${COLIMA_HOME:-}/default/docker.sock" in command
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


def test_ssh_probe_uses_configured_connect_target_and_port(monkeypatch) -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    manager.ssh_identity_file = ""
    host = module.HostPolicy(
        name="ai1",
        ssh_target="ai@ai1",
        ssh_connect_target="ai@host.docker.internal",
        ssh_port=19022,
        repo_dir="",
        env_file="",
        platform="linux",
        resource_kind="linux_nvidia",
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert asyncio.run(manager._ssh(host, "hostname")) == "ok"

    args = calls[0]
    assert args[args.index("-p") + 1] == "19022"
    assert "ai@host.docker.internal" in args
    assert "ai@ai1" not in args
