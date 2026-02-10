# Deployment Manifests

This directory provides per-service manifests for Docker Compose and containerd (via nerdctl).

## Docker Compose

```bash
# Create the shared network
docker network create nexus

# Start gateway + etcd
cd deploy/docker-compose

docker compose -f gateway.yml up -d
```

## containerd (nerdctl)

```bash
# Create the shared network
nerdctl network create nexus

# Start gateway + etcd
cd deploy/containerd

nerdctl compose -f gateway.yml up -d
```

## Setup and Deployment Scripts

Make sure helper scripts are executable before first use:

```bash
chmod +x ../quickstart.sh ./scripts/*.sh
```

Script entrypoints:

- `../quickstart.sh`: interactive local bootstrap (preflight + `.env` + startup)
- `./scripts/preflight-check.sh`: host validation for required tools/files/permissions
- `./scripts/deploy.sh <dev|prod> <branch>`: deploy current repo on a host
- `./scripts/remote-deploy.sh <dev|prod> <branch> <user@host>`: deploy over SSH
- `./scripts/register-service.sh <name> <base-url> <etcd-url>`: register backend in etcd
- `./scripts/list-services.sh <etcd-url>`: inspect registered services

## Notes

- These manifests assume a shared `nexus` network for multi-host deployments.
- Update base URLs (e.g., `OLLAMA_BASE_URL`) to point to remote services when running across hosts.
- The UI is intentionally separated from the gateway for production deployments; keep it as a standalone container when it is implemented.
- For branch-based deploys, see `deploy/scripts/deploy.sh` and `deploy/scripts/remote-deploy.sh`.
- For etcd convenience, use `deploy/scripts/register-service.sh` and `deploy/scripts/list-services.sh`.
