from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_meltdown_is_tracked_as_linux_nvidia_topology_host() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    meltdown = topology["hosts"]["meltdown"]

    assert meltdown["platform"] == "linux"
    assert meltdown["resource_kind"] == "linux_nvidia"
    assert meltdown["ssh_target"] == "ai@meltdown"
    assert meltdown["components"] == []
    assert lifecycle["hosts"]["meltdown"]["resource_kind"] == "linux_nvidia"
    assert lifecycle["hosts"]["meltdown"]["env_file"] == "deploy/env/.env.prod.meltdown"


def test_meltdown_container_alias_is_available_to_control_plane_compose() -> None:
    for compose_file in (
        "docker-compose.gateway.yml",
        "docker-compose.lifecycle-manager.yml",
        "docker-compose.nginx.yml",
        "compose/nginx.yml",
    ):
        text = _read(compose_file)
        assert "meltdown:${NEXUS_HOST_MELTDOWN_IP}" in text
        assert "meltdown.embrient.com:${NEXUS_HOST_MELTDOWN_IP}" in text

    assert "NEXUS_HOST_MELTDOWN_IP=10.10.22.186" in _read(".env.example")
    assert "NEXUS_HOST_MELTDOWN_IP" in _read("deploy/scripts/_common.sh")


def test_ansible_wrapper_exposes_meltdown_host() -> None:
    wrapper = _read("deploy/scripts/ansible-topology.sh")

    assert "ai1|ai2|ada2|meltdown" in wrapper
    assert "bootstrap meltdown" in wrapper
