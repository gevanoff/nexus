from __future__ import annotations

import asyncio
import importlib.util
import json
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


def test_filesystem_probe_is_parsed_into_host_status() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    host = module.HostPolicy(
        name="stackrot",
        ssh_target="ai@stackrot",
        ssh_connect_target="",
        ssh_port=22,
        repo_dir="",
        env_file="",
        platform="linux",
        resource_kind="linux_nvidia",
    )

    manager._parse_linux_probe(
        host,
        "\n".join(
            [
                "__OS__",
                "name=Linux",
                "__MEM__",
                "1000 500 500",
                "__FILESYSTEMS__",
                "mount=/\tfilesystem=/dev/nvme0n1p2\ttotal_kb=102400\tused_kb=92160\tavailable_kb=10240\tused_pct=90",
                "__DOCKER__",
            ]
        ),
    )

    assert host.filesystems == [
        {
            "mount": "/",
            "filesystem": "/dev/nvme0n1p2",
            "total_mb": 100,
            "used_mb": 90,
            "available_mb": 10,
            "used_pct": 90,
        }
    ]


def test_unused_docker_image_prune_is_bounded_and_requires_confirmation() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    host = module.HostPolicy(
        name="stackrot",
        ssh_target="ai@stackrot",
        ssh_connect_target="",
        ssh_port=22,
        repo_dir="",
        env_file="",
        platform="linux",
        resource_kind="linux_nvidia",
    )
    manager.hosts = {"stackrot": host}
    calls = []

    async def fake_ssh(target, command, *, timeout=30):
        calls.append((target, command, timeout))
        return "Total reclaimed space: 12.5GB\n"

    async def fake_refresh():
        return {}

    manager._ssh = fake_ssh
    manager.refresh = fake_refresh

    unconfirmed = asyncio.run(
        manager.prune_unused_docker_images(
            module.HostDockerImagePruneRequest(host="stackrot")
        )
    )
    assert unconfirmed["decision"] == "requires_confirmation"
    assert calls == []

    result = asyncio.run(
        manager.prune_unused_docker_images(
            module.HostDockerImagePruneRequest(host="stackrot", confirmed=True)
        )
    )
    assert result["reclaimed"] == "12.5GB"
    assert len(calls) == 1
    assert "image prune -a -f" in calls[0][1]
    assert "volume" not in calls[0][1]
    assert calls[0][2] == 600


def test_component_container_match_allows_compose_suffixes() -> None:
    module = _load_lifecycle_main()

    assert module.LifecycleManager._component_container_active("nexus-gateway", "nexus-gateway")
    assert module.LifecycleManager._component_container_active("nexus-gateway-1", "nexus-gateway")
    assert not module.LifecycleManager._component_container_active(
        "nexus-gateway-registrar-1",
        "nexus-gateway",
    )


def test_container_status_requires_healthy_when_health_is_reported() -> None:
    module = _load_lifecycle_main()

    assert module.LifecycleManager._container_status_ready("Up 2 hours")
    assert module.LifecycleManager._container_status_ready("Up 2 hours (healthy)")
    assert not module.LifecycleManager._container_status_ready("Up 2 hours (unhealthy)")
    assert not module.LifecycleManager._container_status_ready("Up 5 seconds (health: starting)")
    assert not module.LifecycleManager._container_status_ready("Exited (1) 2 hours ago")


def test_core_service_status_reports_unhealthy_container_as_red() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    manager.hosts = {
        "ada2": module.HostPolicy(
            name="ada2",
            ssh_target="ai@ada2",
            ssh_connect_target="",
            ssh_port=22,
            repo_dir="",
            env_file="",
            platform="linux",
            resource_kind="linux_nvidia",
            containers={"nexus-telegram-bot-ada2": "Up 3 minutes (unhealthy)"},
            updated_at=123.0,
        )
    }
    service = module.CoreServicePolicy(
        service_id="telegram_bridge_tess",
        display_name="Tess Telegram Bridge Runtime",
        host="ada2",
        components=["telegram-bot-ada2"],
        tier="core",
        notes="",
    )

    status = manager._core_service_status(service)

    assert status["active"] is False
    assert status["status"] == "unhealthy"
    assert status["status_color"] == "red"


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


def test_mlx_cache_purge_runs_guarded_host_script(monkeypatch) -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    backend = SimpleNamespace(
        backend_class="local_mlx",
        host="ai2",
        last_action="",
        last_action_at=0.0,
        last_action_error="",
    )
    host = module.HostPolicy(
        name="ai2",
        ssh_target="ai@ai2",
        ssh_connect_target="",
        ssh_port=22,
        repo_dir="/ai-data/var/lib/nexus",
        env_file="",
        platform="macos",
        resource_kind="macos",
    )
    calls = []

    manager.hosts = {"ai2": host}
    manager._backend_or_404 = lambda _backend: backend
    manager._save_state = lambda: None
    manager._backend_status = lambda _backend: {"backend_class": "local_mlx"}

    async def fake_ssh(target, command, *, timeout=30):
        calls.append((target, command, timeout))
        return "removed_path=/ai-data/huggingface/models--mlx-community--GLM-5.2-4bit\nremoved_count=1\n"

    manager._ssh = fake_ssh
    payload = asyncio.run(
        manager.purge_mlx_model_cache(
            module.MlxPrefetchRequest(model="mlx-community/GLM-5.2-4bit")
        )
    )

    assert payload["decision"] == "cache_purged"
    assert payload["removed_paths"] == ["/ai-data/huggingface/models--mlx-community--GLM-5.2-4bit"]
    assert calls[0][0] is host
    assert "./deploy/scripts/purge-mlx-model-cache.sh --model mlx-community/GLM-5.2-4bit" in calls[0][1]
    assert calls[0][2] == 600


def test_mlx_cache_redownload_purges_before_prefetch() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    calls = []

    async def fake_purge(req):
        calls.append(("purge", req.model))
        return {"ok": True, "removed_paths": ["cache"]}

    async def fake_prefetch(req):
        calls.append(("prefetch", req.model))
        return {"ok": True, "pid": "1234", "log_path": "/tmp/prefetch.log"}

    manager.purge_mlx_model_cache = fake_purge
    manager.prefetch_mlx_model = fake_prefetch
    payload = asyncio.run(
        manager.redownload_mlx_model(
            module.MlxPrefetchRequest(model="mlx-community/GLM-5.2-4bit")
        )
    )

    assert calls == [
        ("purge", "mlx-community/GLM-5.2-4bit"),
        ("prefetch", "mlx-community/GLM-5.2-4bit"),
    ]
    assert payload["decision"] == "redownload_started"
    assert payload["pid"] == "1234"


def test_mlx_prefetch_uses_tracked_retrying_worker() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    backend = SimpleNamespace(
        backend_class="local_mlx",
        host="ai2",
        last_action="",
        last_action_at=0.0,
        last_action_error="",
    )
    host = module.HostPolicy(
        name="ai2",
        ssh_target="ai@ai2",
        ssh_connect_target="",
        ssh_port=22,
        repo_dir="/ai-data/var/lib/nexus",
        env_file="",
        platform="macos",
        resource_kind="macos",
    )
    calls = []
    manager.hosts = {"ai2": host}
    manager._backend_or_404 = lambda _backend: backend
    manager._save_state = lambda: None
    manager._backend_status = lambda _backend: {"backend_class": "local_mlx"}
    manager.mlx_prefetch_max_attempts = 5
    manager.mlx_prefetch_retry_base_sec = 30
    manager.mlx_prefetch_retry_max_sec = 300
    manager.mlx_prefetch_progress_interval_sec = 5

    async def fake_ssh(target, command, *, timeout=30):
        calls.append((target, command, timeout))
        return "2468\n"

    manager._ssh = fake_ssh
    payload = asyncio.run(
        manager.prefetch_mlx_model(module.MlxPrefetchRequest(model="mlx-community/GLM-5.2-4bit"))
    )

    assert payload["decision"] == "prefetch_started"
    assert payload["pid"] == "2468"
    assert payload["max_attempts"] == 5
    command = calls[0][1]
    assert "/ai-data/var/lib/nexus/services/mlx/scripts/prefetch-models.sh" in command
    assert "--max-attempts 5" in command
    assert "--retry-base-sec 30" in command
    assert "--retry-max-sec 300" in command
    assert "--progress-interval-sec 5" in command
    assert calls[0][2] == 30


def test_mlx_huge_switch_runs_guarded_host_operation() -> None:
    module = _load_lifecycle_main()
    manager = object.__new__(module.LifecycleManager)
    backend = SimpleNamespace(
        backend_class="local_mlx",
        host="ai2",
        last_action="",
        last_action_at=0.0,
        last_action_error="",
    )
    host = module.HostPolicy(
        name="ai2",
        ssh_target="ai@ai2",
        ssh_connect_target="",
        ssh_port=22,
        repo_dir="/ai-data/var/lib/nexus",
        env_file="",
        platform="macos",
        resource_kind="macos",
    )
    calls = []

    manager.hosts = {"ai2": host}
    manager._backend_or_404 = lambda _backend: backend
    manager._save_state = lambda: None
    manager._backend_status = lambda _backend: {"backend_class": "local_mlx", "ready": True}

    async def fake_ssh(target, command, *, timeout=30):
        calls.append((target, command, timeout))
        return "resident_model=mlx-community/GLM-5.2-4bit\n"

    async def fake_refresh():
        return None

    manager._ssh = fake_ssh
    manager.refresh = fake_refresh
    payload = asyncio.run(
        manager.switch_mlx_huge_model(
            module.MlxPrefetchRequest(model="mlx-community/GLM-5.2-4bit")
        )
    )

    assert payload["decision"] == "resident_huge_model_ready"
    assert "./deploy/scripts/switch-mlx-huge-model.sh --model mlx-community/GLM-5.2-4bit" in calls[0][1]
    assert calls[0][2] == 3900


def test_mlx_prefetch_forces_hugging_face_online_mode() -> None:
    root = Path(__file__).resolve().parents[3]
    wrapper = (root / "services" / "mlx" / "scripts" / "prefetch-models.sh").read_text(encoding="utf-8")
    helper = (root / "services" / "mlx" / "scripts" / "prefetch_models.py").read_text(encoding="utf-8")

    assert wrapper.index('HF_HUB_OFFLINE=0') > wrapper.index('. "$MLX_ENV_FILE"')
    assert "export HOME XDG_CACHE_HOME HF_HOME HF_HUB_OFFLINE" in wrapper
    assert helper.index('os.environ["HF_HUB_OFFLINE"] = "0"') < helper.index("from huggingface_hub import HfApi, snapshot_download")


def test_mlx_lifecycle_canary_does_not_probe_an_on_demand_huge_model() -> None:
    root = Path(__file__).resolve().parents[3]
    policy = json.loads(
        (root / "deploy" / "topology" / "backend_lifecycle.json").read_text(encoding="utf-8")
    )

    canary = policy["backends"]["local_mlx"]["canary"]
    assert canary["enabled"] is False
    assert canary["payload"]["model"] == "mlx-community/GLM-5.2-4bit"


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
