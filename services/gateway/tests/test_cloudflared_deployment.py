from __future__ import annotations

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


def test_cloudflared_deploy_script_is_valid_bash_and_materializes_secret():
    script = ROOT / "deploy/scripts/deploy-cloudflared.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")

    assert "CLOUDFLARED_TUNNEL_TOKEN is missing" in text
    assert 'token_path="$token_dir/tunnel-token"' in text
    assert 'chmod 700 "$token_dir"' in text
    assert 'chmod 444 "$token_tmp"' in text
    assert "unset tunnel_token token_tmp" in text
    assert "docker-compose.gateway.yml" in text
    assert "docker-compose.etcd.yml" in text
    assert "docker-compose.cloudflared.yml" in text
    assert "pull cloudflared" in text
    assert "--force-recreate gateway cloudflared" in text


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
