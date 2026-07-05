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


def test_docker_probe_uses_canonical_colima_context() -> None:
    module = _load_lifecycle_main()
    command = module.LifecycleManager._docker_probe_command()

    assert "ps --format" in command
    assert "context_output" in command
    assert "default_output" in command
    assert "${DOCKER_CONTEXT:-colima}" in command
    assert '--context "$colima_context"' in command
    assert "DOCKER_HOST" not in command
    assert "/ai-data/var/lib/colima" not in command


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
        name="stackrot",
        ssh_target="ai@stackrot",
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
    assert "ai@stackrot" not in args


def test_runtime_env_base_url_overrides_topology_default(monkeypatch, tmp_path) -> None:
    module = _load_lifecycle_main()
    policy_path = tmp_path / "backend_lifecycle.json"
    topology_path = tmp_path / "production.json"
    state_path = tmp_path / "state.json"
    policy_path.write_text(
        """
{
  "settings": {"mode": "assisted"},
  "backends": {
    "local_vllm_fast": {
      "host": "stackrot",
      "component": "vllm-fast"
    }
  }
}
""",
        encoding="utf-8",
    )
    topology_path.write_text(
        """
{
  "defaults": {
    "env": {
      "VLLM_FAST_BASE_URL": "http://stackrot:8001/v1"
    }
  },
  "hosts": {
    "stackrot": {"platform": "linux", "ssh_target": "ai@stackrot"}
  }
}
""",
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NEXUS_LIFECYCLE_POLICY", str(policy_path))
    monkeypatch.setenv("NEXUS_TOPOLOGY_FILE", str(topology_path))
    monkeypatch.setenv("NEXUS_LIFECYCLE_STATE_PATH", str(state_path))
    monkeypatch.setenv("VLLM_FAST_BASE_URL", "http://host.docker.internal:18001/v1")

    manager = module.LifecycleManager()

    assert manager.backends["local_vllm_fast"].base_url == "http://host.docker.internal:18001/v1"


def test_network_probe_parses_current_and_theoretical_speeds() -> None:
    module = _load_lifecycle_main()

    interfaces = module.LifecycleManager._parse_network_interfaces(
        [
            "name=enp7s0\tmac=00:11:22:33:44:55\toperstate=up\tcarrier=1\tcurrent_speed_mbps=1000\tduplex=full\tcurrent_media=1000Mb/s Full Twisted Pair\tsupported_media=1000baseT/Full 2500baseT/Full 10000baseT/Full",
            "name=en0\tdisplay_name=Ethernet\tmac=aa:bb:cc:dd:ee:ff\toperstate=active\tcurrent_media=autoselect (1000baseT <full-duplex>)\tsupported_media=media 1000baseT mediaopt full-duplex media 10Gbase-T mediaopt full-duplex",
        ]
    )

    assert interfaces[0]["name"] == "en0"
    assert interfaces[0]["display_name"] == "Ethernet"
    assert interfaces[0]["active"] is True
    assert interfaces[0]["current_speed_mbps"] == 1000
    assert interfaces[0]["theoretical_speed_mbps"] == 10000
    assert interfaces[1]["name"] == "enp7s0"
    assert interfaces[1]["current_speed_mbps"] == 1000
    assert interfaces[1]["theoretical_speed_mbps"] == 10000
