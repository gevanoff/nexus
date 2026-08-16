from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_cloudflared_compose_uses_token_file_and_is_not_host_exposed():
    text = (ROOT / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")

    assert "cloudflare/cloudflared:${CLOUDFLARED_IMAGE_TAG:-2026.7.2}" in text
    assert "--token-file" in text
    assert "/run/secrets/cloudflared_tunnel_token" in text
    assert "TUNNEL_TOKEN:" not in text
    assert "ports:" not in text
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
    assert "cap_drop:" in text and "- ALL" in text


def test_tunnel_origin_network_has_fixed_connector_identity():
    text = (ROOT / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")

    assert "nexus-gateway-tunnel" in text
    assert "CLOUDFLARED_GATEWAY_IP:-172.29.0.2" in text
    assert "CLOUDFLARED_CONNECTOR_IP:-172.29.0.3" in text
    assert "UI_IP_ALLOWLIST=${UI_IP_ALLOWLIST:-127.0.0.1},${CLOUDFLARED_CONNECTOR_IP:-172.29.0.3}" in text
    assert "internal: true" in text
    assert "CLOUDFLARED_ORIGIN_SUBNET:-172.29.0.0/29" in text


def test_canonical_deploy_engine_owns_cloudflared_orchestration():
    script = ROOT / "deploy/scripts/deploy.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")

    assert "deployment-control|gateway|cloudflared|vllm" in text
    assert 'cloudflared) echo "docker-compose.gateway.yml"' in text
    assert 'cloudflared) echo "docker-compose.cloudflared.yml"' in text
    assert 'prepare_cloudflared_runtime "$env_file" "$host_runtime_root"' in text
    assert "CLOUDFLARED_TUNNEL_TOKEN is required when cloudflared is selected" in text
    assert 'token_path="$token_dir/tunnel-token"' in text
    assert 'chmod 700 "$token_dir"' in text
    assert 'chmod 444 "$token_tmp"' in text
    assert "unset tunnel_token token_tmp" in text


def test_ai2_gateway_uses_cloudflare_overlay_without_selecting_cloudflared_service():
    script = ROOT / "deploy/scripts/deploy.sh"
    text = script.read_text(encoding="utf-8")

    assert (
        'if [[ "${TOPOLOGY_HOST:-}" == "ai2" ]] && component_selected gateway '
        '&& ! component_selected cloudflared; then'
    ) in text
    assert 'cloudflared_overlay_file="docker-compose.cloudflared.yml"' in text
    assert 'append_compose_file_unique "$cloudflared_overlay_file"' in text
    assert 'service_targets+=("$service_name")' in text
    assert '"${up_args[@]}" "${service_targets[@]}"' in text
    assert (
        "Gateway Cloudflare overlay enabled; cloudflared remains running unless explicitly selected."
        in text
    )

    explicit_scope = text.split(
        'if [[ "$EXPLICIT_COMPONENTS_SET" == "true" ]]; then', 1
    )[1].split("  else\n    up_args+=(--remove-orphans)", 1)[0]
    assert 'if [[ "$gateway_cloudflared_overlay" == "true" ]]; then' in explicit_scope
    assert explicit_scope.index('if [[ "$gateway_cloudflared_overlay" == "true" ]]; then') < explicit_scope.index(
        'service_targets+=("$service_name")'
    )


def test_deployment_control_client_keeps_requested_components_component_scoped():
    module = runpy.run_path(str(ROOT / "deploy/scripts/deployment-control-client.py"))
    normalize_components = module["normalize_components"]

    assert normalize_components(["gateway", " gateway ", ""]) == ["gateway"]
    assert normalize_components(["gateway"]) == ["gateway"]
    assert "expand_component_dependencies" not in module


def test_cloudflared_compatibility_script_delegates_to_deploy_engine():
    script = ROOT / "deploy/scripts/deploy-cloudflared.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")

    assert "Compatibility wrapper for the canonical deployment engine" in text
    assert "request-deploy.sh" in text
    assert "--components cloudflared" in text
    assert 'exec "$ROOT_DIR/deploy/scripts/deploy.sh"' in text
    assert "docker compose" not in text.lower()
    assert "token_path=" not in text


def test_cloudflared_is_assigned_to_ai2_and_controller_allowlist():
    topology = json.loads((ROOT / "deploy/topology/production.json").read_text(encoding="utf-8"))
    ai2 = topology["hosts"]["ai2"]
    copyfail = topology["hosts"]["copyfail"]
    controller_compose = (ROOT / "docker-compose.deployment-control.yml").read_text(encoding="utf-8")

    assert "cloudflared" in ai2["components"]
    assert ai2["env"]["DEPLOY_CONTROL_BASE_URL"] == "http://copyfail:9220"
    assert ai2["env"]["CLOUDFLARED_CONNECTOR_IP"] == "172.29.0.3"
    assert copyfail["env"]["DEPLOY_CONTROL_BIND_ADDRESS"] == "0.0.0.0"
    assert "ace-step,cloudflared,deployment-control" in controller_compose


def test_shadowrepository_hostname_is_consistent():
    expected = "https://nexus.shadowrepository.org"
    social_env = (ROOT / "deploy/env/social-publishing.example").read_text(encoding="utf-8")
    tunnel_env = (ROOT / "deploy/env/cloudflared.example").read_text(encoding="utf-8")
    documentation = (ROOT / "docs/CLOUDFLARE_TUNNEL.md").read_text(encoding="utf-8")

    assert expected in social_env
    assert expected in tunnel_env
    assert expected in documentation
    assert "nexus.shadowrepository.org/social-media/*" in documentation
    assert "nexus.shadowrepository.org/ui/social/oauth/youtube/callback" in documentation
    assert "nexus.shadowrepository.org/ui/social/oauth/meta/callback" in documentation
    assert "nexus.shadowrepository.org/ui/social/oauth/tiktok/callback" in documentation


def test_cloudflare_is_documented_as_optional_for_assisted_publishing():
    tunnel_env = (ROOT / "deploy/env/cloudflared.example").read_text(encoding="utf-8")
    documentation = (ROOT / "docs/CLOUDFLARE_TUNNEL.md").read_text(encoding="utf-8")

    assert "Assisted social publishing does not require" in tunnel_env
    assert "Cloudflare Tunnel is optional" in documentation
    assert "It is **not** required for assisted social publishing" in documentation
    assert "When direct Instagram publishing is not enabled, omit this Bypass application" in documentation
