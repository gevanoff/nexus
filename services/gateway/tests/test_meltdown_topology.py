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

    assert "sdxl-turbo" not in topology["hosts"]["stackrot"]["components"]
    assert "vllm-embeddings" not in topology["hosts"]["stackrot"]["components"]
    assert topology["defaults"]["env"]["SDXL_TURBO_BASE_URL"] == "http://meltdown:9050"
    assert topology["defaults"]["env"]["VLLM_EMBEDDINGS_BASE_URL"] == "http://meltdown:8002/v1"
    assert topology["hosts"]["meltdown"]["env"]["SDXL_TURBO_CUDA_VISIBLE_DEVICES"] == "0"
    assert topology["hosts"]["meltdown"]["env"]["VLLM_EMBEDDINGS_CUDA_VISIBLE_DEVICES"] == "0"
    assert lifecycle["backends"]["gpu_fast"]["host"] == "meltdown"
    assert lifecycle["backends"]["local_vllm_embeddings"]["host"] == "meltdown"


def test_stackrot_owns_tts_stack_on_second_gpu() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    stackrot = topology["hosts"]["stackrot"]
    ai2 = topology["hosts"]["ai2"]
    defaults = topology["defaults"]["env"]

    assert {"tts", "luxtts", "qwen3-tts"}.issubset(stackrot["components"])
    assert {"tts", "luxtts", "qwen3-tts"}.isdisjoint(ai2["components"])
    assert stackrot["env"]["QWEN3_TTS_CUDA_VISIBLE_DEVICES"] == "1"
    assert stackrot["env"]["QWEN3_TTS_DEVICE_MAP"] == "cuda:0"
    assert defaults["POCKET_TTS_BASE_URL"] == "http://stackrot:9940"
    assert defaults["LUXTTS_BASE_URL"] == "http://stackrot:9170"
    assert defaults["QWEN3_TTS_BASE_URL"] == "http://stackrot:9175"
    assert lifecycle["backends"]["pocket_tts"]["host"] == "stackrot"
    assert lifecycle["backends"]["luxtts"]["host"] == "stackrot"
    assert lifecycle["backends"]["qwen3_tts"]["host"] == "stackrot"
    assert "gpus: all" in _read("docker-compose.qwen3-tts.yml")


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
        assert "copyfail:${NEXUS_HOST_COPYFAIL_IP}" not in text
        assert "copyfail.embrient.com:${NEXUS_HOST_COPYFAIL_IP}" not in text

    env_example = _read(".env.example")
    assert "\nNEXUS_HOST_MELTDOWN_IP=\n" in env_example
    assert "\nNEXUS_HOST_COPYFAIL_IP=\n" in env_example
    assert "NEXUS_HOST_MELTDOWN_IP" in _read("deploy/scripts/_common.sh")
    assert "NEXUS_HOST_COPYFAIL_IP" in _read("deploy/scripts/_common.sh")


def test_ai2_uses_boot_persistent_proxies_and_physical_topology_mount() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    ai2_env = topology["hosts"]["ai2"]["env"]
    installer = _read("deploy/scripts/install-backend-port-proxy-launchd.sh")

    assert ai2_env["NEXUS_TOPOLOGY_HOST_DIR"] == "/Volumes/ai_data/var/lib/nexus/deploy/topology"
    assert "${NEXUS_TOPOLOGY_HOST_DIR:-./deploy/topology}:/app/config:ro" in _read(
        "docker-compose.lifecycle-manager.yml"
    )
    assert "/Library/LaunchDaemons/${LABEL}.plist" in installer
    for forward in (
        "images=127.0.0.1:17860=ada2:7860",
        "sdxl-turbo=127.0.0.1:18050=meltdown:9050",
        "lighton-ocr=127.0.0.1:18155=ada2:9155",
        "skyreels-v2=127.0.0.1:18180=ada2:9180",
        "ssh-copyfail=127.0.0.1:19025=copyfail:22",
    ):
        assert forward in installer


def test_ansible_wrapper_exposes_meltdown_host() -> None:
    wrapper = _read("deploy/scripts/ansible-topology.sh")

    assert "stackrot | ai2 | ada2 | meltdown | migraine | copyfail" in wrapper
    assert "copyfail" in wrapper
    assert "bootstrap meltdown" in wrapper
    assert "bootstrap migraine" in wrapper
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


def test_honcho_stack_keeps_storage_private_and_inference_remote() -> None:
    compose = _read("docker-compose.honcho.yml")
    config = _read("deploy/honcho/config.nexus.toml")
    docs = _read("deploy/honcho/README.md")
    memory_policy = _read("deploy/honcho/MEMORY_POLICY.md")

    assert "container_name: nexus-honcho" in compose
    assert "container_name: nexus-honcho-deriver" in compose
    assert "container_name: nexus-honcho-database" in compose
    assert "container_name: nexus-honcho-redis" in compose
    assert "POSTGRES_HOST_AUTH_METHOD=trust" not in compose
    assert "HONCHO_DB_PASSWORD" in compose
    assert "AUTH_USE_AUTH=true" in _read("deploy/honcho/honcho.env.example")
    assert 'base_url = "http://ai2:8800/v1"' in config
    assert 'model = "embeddings"' in config
    assert "VECTOR_DIMENSIONS = 384" in config
    assert "v3.0.12" in docs
    assert "nexus:{nexus_user_id}:telegram:{telegram_user_id}" in memory_policy
    assert "telegram:private:{chat_id}:bot:{bot_id}" in memory_policy
    assert "shared long-term conclusions" in memory_policy
    assert "does not grant download access" in memory_policy
    assert "HONCHO_MEMORY_ENABLED=true" in docs
    assert "--workspace nexus --print-only" in docs
    assert "--expires 1y" not in docs


def test_migraine_owns_one_constrained_native_mlx_backend() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    migraine = topology["hosts"]["migraine"]
    assert migraine["platform"] == "macos"
    assert migraine["resource_kind"] == "macos"
    assert migraine["components"] == []
    assert migraine["native_services"] == ["mlx"]
    assert migraine["env"]["MLX_PORT"] == "10241"
    assert lifecycle["hosts"]["migraine"]["platform"] == "macos"
    assert lifecycle["hosts"]["migraine"]["resource_kind"] == "macos"
    assert lifecycle["hosts"]["migraine"]["ssh_target"] == "ai@migraine"
    assert lifecycle["core_services"]["hermes_client"]["host"] == "migraine"
    assert lifecycle["core_services"]["hermes_client"]["components"] == []
    assert "existing local SOUL.md" in lifecycle["core_services"]["hermes_client"]["notes"]
    assert lifecycle["backends"]["local_mlx_migraine"]["host"] == "migraine"
    assert lifecycle["backends"]["local_mlx_migraine"]["compose_managed"] is False
    assert lifecycle["backends"]["local_mlx_migraine"]["estimated_vram_mb"] == 2600


def test_host_telegram_bots_use_distinct_tokens_models_and_identities() -> None:
    compose = _read("docker-compose.telegram-bot.yml")
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    assert "TELEGRAM_AI2_TOKEN" in compose
    assert "TELEGRAM_ADA2_TOKEN" in compose
    assert "TELEGRAM_STACKROT_TOKEN" in compose
    assert "TELEGRAM_MELTDOWN_TOKEN" in compose
    assert "ai2-chat" in compose
    assert "ada2-chat" in compose
    assert "stackrot-chat" in compose
    assert "cinder-chat" in compose
    assert "TELEGRAM_ADA2_GATEWAY_BASE_URL" in compose
    assert "TELEGRAM_MELTDOWN_GATEWAY_BASE_URL" in compose
    assert "http://ai2.embrient.com:8800" in compose
    assert "TELEGRAM_MEMORY_ENABLED" in compose
    assert "TELEGRAM_MEMORY_TIMEOUT_MS" in compose
    assert "telegram-bot-migraine" not in compose
    assert compose.count("profiles: [host-bots]") == 3
    assert "telegram_bot" not in lifecycle["backends"]
    assert lifecycle["core_services"]["telegram_bridge_clarion"]["component"] == "telegram-bot"
    assert lifecycle["core_services"]["telegram_bridge_tess"]["component"] == "telegram-bot-ada2"
    assert lifecycle["core_services"]["telegram_bridge_hex"]["component"] == "telegram-bot-stackrot"
    assert lifecycle["core_services"]["telegram_bridge_cinder"]["component"] == "telegram-bot-meltdown"

    topology = json.loads(_read("deploy/topology/production.json"))
    assert (
        topology["hosts"]["ada2"]["env"]["TELEGRAM_ADA2_GATEWAY_BASE_URL"]
        == "http://ai2.embrient.com:8800"
    )

    aliases = json.loads(_read("services/gateway/app/model_aliases.json"))["aliases"]
    assert aliases["cinder-chat"]["backend"] == "local_vllm_fast"
    assert aliases["cinder-chat"]["soul"] == "meltdown"


def test_adada_is_lifecycle_only_inventory_host() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    assert "adada" not in topology["hosts"]
    assert lifecycle["hosts"]["adada"]["platform"] == "linux"
    assert lifecycle["hosts"]["adada"]["resource_kind"] == "linux_nvidia"
    assert lifecycle["hosts"]["adada"]["ssh_target"] == "ai@adada"
    assert lifecycle["core_services"]["adada_inventory"]["host"] == "adada"
    assert lifecycle["core_services"]["adada_inventory"]["components"] == []

    for backend in lifecycle["backends"].values():
        assert backend.get("host") != "adada"


def test_ai2_lifecycle_uses_host_side_ssh_proxies_for_remote_hosts() -> None:
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))
    installer = _read("deploy/scripts/install-backend-port-proxy-launchd.sh")
    expected_ports = {
        "stackrot": 19022,
        "ada2": 19023,
        "meltdown": 19024,
        "copyfail": 19025,
    }

    for host, port in expected_ports.items():
        assert lifecycle["hosts"][host]["ssh_connect_target"] == "ai@host.docker.internal"
        assert lifecycle["hosts"][host]["ssh_port"] == port
        assert f"ssh-{host}=127.0.0.1:{port}={host}:22" in installer


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


def test_deploy_script_uses_physical_repo_root_for_colima_binds() -> None:
    deploy_script = _read("deploy/scripts/deploy.sh")

    assert 'pwd -P)' in deploy_script
    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"' in deploy_script


def test_macos_docker_resources_share_one_colima_profile_home_and_context() -> None:
    group_vars = _read("ansible/inventory/group_vars/all.yml")
    macos_role = _read("ansible/roles/nexus_docker_runtime/tasks/macos.yml")
    common = _read("deploy/scripts/_common.sh")
    launch_agent = _read("deploy/scripts/colima-launch-agent.sh")
    installer = _read("deploy/scripts/install-colima-launchd.sh")
    lifecycle_restart = _read("deploy/scripts/restart-lifecycle-manager.sh")

    assert "nexus_colima_profile: default" in group_vars
    assert 'nexus_colima_home: "{{ nexus_user_home }}/.colima"' in group_vars
    assert "--colima-home" in macos_role
    assert 'DOCKER_CONTEXT="$(ns_colima_context_for_profile "$COLIMA_PROFILE")"' in common
    assert "COLIMA_HOME=\"${COLIMA_HOME:-${HOME:-}/.colima}\"" in common
    assert 'resolved_path="$(ns_resolve_docker_bind_path "$env_file")"' in common
    assert 'stop_cmd=("$COLIMA_BIN" stop --force --profile "$COLIMA_PROFILE")' in launch_agent
    assert "restart-ai2-services.sh" not in launch_agent
    assert "qemu fallback" not in launch_agent
    assert "COLIMA_FALLBACK_HOME" not in launch_agent
    assert "DOCKER_CONTEXT=${DOCKER_CONTEXT}" in installer
    assert '"${HOME:-}/.colima"' in lifecycle_restart
    assert "/ai-data/var/lib/nexus-colima" not in lifecycle_restart
    assert "DOCKER_HOST=" not in lifecycle_restart
