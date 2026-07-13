import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ops_stack_is_a_thin_topology_aware_deploy_wrapper():
    script = _read("deploy/scripts/ops-stack.sh")

    assert 'exec "$ROOT_DIR/deploy/scripts/deploy.sh"' in script
    assert '--topology-host is required' in script
    assert "ns_compose" not in script
    assert "docker-compose." not in script
    assert "git pull" not in script


def test_ops_stack_rejects_retired_ambiguous_flags():
    script = _read("deploy/scripts/ops-stack.sh")

    for option in (
        "--no-pull",
        "--no-build",
        "--external-vllm",
        "--external-mlx",
        "--with-mlx",
        "--with-telegram",
    ):
        assert option in script
    assert "legacy_option_error" in script


@pytest.mark.skipif(os.name == "nt", reason="shell entrypoint execution is covered in Linux CI")
def test_ops_stack_forwards_topology_and_component_arguments(tmp_path):
    scripts_dir = tmp_path / "deploy" / "scripts"
    scripts_dir.mkdir(parents=True)
    wrapper = scripts_dir / "ops-stack.sh"
    wrapper.write_text(_read("deploy/scripts/ops-stack.sh"), encoding="utf-8")
    wrapper.chmod(0o755)
    deploy = scripts_dir / "deploy.sh"
    deploy.write_text('#!/usr/bin/env bash\nprintf "ARG=%s\\n" "$@"\n', encoding="utf-8")
    deploy.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(wrapper),
            "--topology-host",
            "ai2",
            "--component",
            "gateway",
            "--branch",
            "review",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    forwarded = [line.removeprefix("ARG=") for line in result.stdout.splitlines() if line.startswith("ARG=")]
    assert forwarded == ["--topology-host", "ai2", "--component", "gateway", "prod", "review"]


def test_retired_stack_orchestration_scripts_are_removed():
    for path in (
        "deploy/scripts/backup-and-deploy-parallel.sh",
        "deploy/scripts/cutover-one-way.sh",
        "deploy/scripts/cutover-tts-one-way.sh",
        "deploy/scripts/migrate-from-ai-infra.sh",
        "deploy/scripts/prewarm-models.sh",
        "deploy/scripts/restart-ai2-services.sh",
        "deploy/scripts/stop-stack.sh",
    ):
        assert not (ROOT / path).exists()


def test_tts_redeploy_cannot_start_gateway_on_backend_host():
    script = _read("deploy/scripts/redeploy-tts-shims.sh")

    assert "docker-compose.gateway.yml" not in script
    assert "up -d gateway" not in script
    assert 'ns_env_get "$ENV_FILE" TTS_PORT' in script
    assert "ns_resolve_docker_bind_path" in script


def test_gateway_verifier_uses_running_container_not_compose_selection():
    script = _read("deploy/scripts/verify-gateway.sh")

    assert 'docker exec -i "$CONTAINER"' in script
    assert "http://127.0.0.1:8800" in script
    assert "http://127.0.0.1:8801" in script
    assert "ns_compose" not in script
    assert "external-vllm" not in script
    assert "with-mlx" not in script


def test_gateway_diagnostics_use_router_alias_for_embeddings():
    script = _read("deploy/scripts/diagnose-gateway.sh")

    assert """'{"model":"default","input":"diagnose"}'""" in script
    assert """'{"model":"embeddings""" not in script


def test_script_map_declares_single_topology_deployment_engine():
    guide = _read("deploy/SCRIPTS.md")

    assert "`deploy.sh` is the only host deployment engine" in guide
    assert "Do not add another script that assembles a production Compose stack" in guide
    assert "Retired Migration And Compatibility Commands" in guide


def test_runtime_root_resolution_is_shared_and_honors_explicit_env_files():
    common = _read("deploy/scripts/_common.sh")

    assert "ns_runtime_root_from_env()" in common
    for path in (
        "deploy/scripts/backup-gateway-db.sh",
        "deploy/scripts/backup-etcd.sh",
        "deploy/scripts/restore-gateway-db.sh",
        "deploy/scripts/restore-etcd.sh",
        "deploy/scripts/redeploy-tts-shims.sh",
        "deploy/scripts/prewarm-mlx.sh",
    ):
        assert 'ns_runtime_root_from_env "$ROOT_DIR" "$ENV_FILE"' in _read(path)

    assert "resolve_runtime_root()" not in _read("deploy/scripts/backup-gateway-db.sh")
    assert "resolve_runtime_root()" not in _read("deploy/scripts/install-etcd-backup-launchd.sh")
    assert "resolve_runtime_root()" not in _read("deploy/scripts/install-gateway-db-backup-launchd.sh")

    for path in (
        "deploy/scripts/backup-gateway-db.sh",
        "deploy/scripts/backup-etcd.sh",
        "deploy/scripts/restore-gateway-db.sh",
        "deploy/scripts/restore-etcd.sh",
        "deploy/scripts/prewarm-mlx.sh",
        "deploy/scripts/redeploy-tts-shims.sh",
    ):
        script = _read(path)
        assert "Env file not found:" in script
        assert "ns_ensure_env_file" not in script


@pytest.mark.skipif(os.name == "nt", reason="shell helper execution is covered in Linux CI")
def test_runtime_root_helper_resolves_relative_path_from_selected_env(tmp_path):
    env_file = tmp_path / "selected.env"
    env_file.write_text("NEXUS_RUNTIME_ROOT=runtime/selected\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; ns_runtime_root_from_env "$2" "$3"',
            "runtime-root-test",
            str(ROOT / "deploy/scripts/_common.sh"),
            str(tmp_path),
            str(env_file),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert Path(result.stdout.strip()) == tmp_path / "runtime/selected"
