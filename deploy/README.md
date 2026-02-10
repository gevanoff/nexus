# Deployment Manifests

This directory provides per-service manifests for Docker Compose and containerd (via nerdctl).

## Docker Compose

```bash
# Create the shared network
docker network create nexus

# Start gateway + etcd
cd deploy/docker-compose

docker-compose -f gateway.yml up -d
```

## containerd (nerdctl)

```bash
# Create the shared network
nerdctl network create nexus

# Start gateway + etcd
cd deploy/containerd

nerdctl compose -f gateway.yml up -d
```

## Notes

- These manifests assume a shared `nexus` network for multi-host deployments.
- Update base URLs (e.g., `OLLAMA_BASE_URL`) to point to remote services when running across hosts.
- The UI is intentionally separated from the gateway for production deployments; keep it as a standalone container when it is implemented.
- For branch-based deploys, see `deploy/scripts/deploy.sh` and `deploy/scripts/remote-deploy.sh`.
- For etcd convenience, use `deploy/scripts/register-service.sh` and `deploy/scripts/list-services.sh`.
