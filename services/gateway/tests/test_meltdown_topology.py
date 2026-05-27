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
    assert meltdown["components"] == ["sdxl-turbo", "vllm-embeddings"]
    assert lifecycle["hosts"]["meltdown"]["resource_kind"] == "linux_nvidia"
    assert lifecycle["hosts"]["meltdown"]["env_file"] == "deploy/env/.env.prod.meltdown"


def test_meltdown_owns_lightweight_gpu_backends() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    assert "sdxl-turbo" not in topology["hosts"]["ai1"]["components"]
    assert "vllm-embeddings" not in topology["hosts"]["ai1"]["components"]
    assert topology["defaults"]["env"]["SDXL_TURBO_BASE_URL"] == "http://meltdown:9050"
    assert topology["defaults"]["env"]["VLLM_EMBEDDINGS_BASE_URL"] == "http://meltdown:8002/v1"
    assert topology["hosts"]["meltdown"]["env"]["SDXL_TURBO_CUDA_VISIBLE_DEVICES"] == "0"
    assert topology["hosts"]["meltdown"]["env"]["VLLM_EMBEDDINGS_CUDA_VISIBLE_DEVICES"] == "0"
    assert lifecycle["backends"]["gpu_fast"]["host"] == "meltdown"
    assert lifecycle["backends"]["local_vllm_embeddings"]["host"] == "meltdown"


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
        assert "copyfail:${NEXUS_HOST_COPYFAIL_IP}" in text
        assert "copyfail.embrient.com:${NEXUS_HOST_COPYFAIL_IP}" in text

    assert "NEXUS_HOST_MELTDOWN_IP=10.10.22.186" in _read(".env.example")
    assert "NEXUS_HOST_COPYFAIL_IP=10.10.22.156" in _read(".env.example")
    assert "NEXUS_HOST_MELTDOWN_IP" in _read("deploy/scripts/_common.sh")
    assert "NEXUS_HOST_COPYFAIL_IP" in _read("deploy/scripts/_common.sh")


def test_ansible_wrapper_exposes_meltdown_host() -> None:
    wrapper = _read("deploy/scripts/ansible-topology.sh")

    assert "ai1|ai2|ada2|meltdown" in wrapper
    assert "copyfail" in wrapper
    assert "bootstrap meltdown" in wrapper
    assert "bootstrap copyfail" in wrapper


def test_meltdown_bootstrap_uses_managed_checkout_and_gpu_runtime_validation() -> None:
    host_vars = _read("ansible/inventory/host_vars/meltdown.yml")
    group_vars = _read("ansible/inventory/group_vars/all.yml")
    linux_docker_role = _read("ansible/roles/nexus_docker_runtime/tasks/linux.yml")

    assert "nexus_manage_checkout: true" in host_vars
    assert "nexus_repo_url: https://github.com/gevanoff/nexus.git" in host_vars
    assert "nexus_nvidia_container_runtime_validate: true" in host_vars
    assert "nexus_linux_docker_official_apt_enabled: true" in group_vars
    assert "docker-ce" in group_vars
    assert "docker-compose-plugin" in group_vars
    assert "https://download.docker.com/linux/${repo_os}" in linux_docker_role
    assert "https://nvidia.github.io/libnvidia-container" in linux_docker_role
    assert "docker info --format" in linux_docker_role
    assert "docker run --rm --gpus all" in linux_docker_role
    assert "become: true" in linux_docker_role


def test_copyfail_is_infra_only_topology_host() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    copyfail = topology["hosts"]["copyfail"]

    assert copyfail["platform"] == "linux"
    assert copyfail["resource_kind"] == "linux_infra"
    assert copyfail["ssh_target"] == "ai@copyfail"
    assert copyfail["components"] == []
    assert "deployment_control" in copyfail["roles"]
    assert lifecycle["hosts"]["copyfail"]["resource_kind"] == "linux_infra"
    assert lifecycle["hosts"]["copyfail"]["env_file"] == "deploy/env/.env.prod.copyfail"
    assert lifecycle["core_services"]["deployment_control"]["host"] == "copyfail"
    assert lifecycle["core_services"]["deployment_control"]["components"] == []

    for backend in lifecycle["backends"].values():
        assert backend.get("host") != "copyfail"


def test_copyfail_ansible_host_vars_skip_model_runtime_install() -> None:
    host_vars = _read("ansible/inventory/host_vars/copyfail.yml")
    preflight_role = _read("ansible/roles/nexus_preflight/tasks/main.yml")
    deploy_role = _read("ansible/roles/nexus_deploy/tasks/main.yml")

    assert "nexus_manage_checkout: true" in host_vars
    assert "nexus_manage_docker_runtime: false" in host_vars
    assert "nexus_manage_nvidia_container_runtime: false" in host_vars
    assert "nexus_manage_mlx_host_prep: false" in host_vars
    assert "ansible" in host_vars
    assert "skipping deploy preflight" in preflight_role
    assert "skipping deploy" in deploy_role
