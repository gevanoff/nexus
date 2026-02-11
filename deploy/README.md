# Deployment Manifests

This directory provides per-service manifests for Docker Compose and containerd (via nerdctl).

## Docker Compose

Use the deployment wrappers instead of manual compose command sequences. From the repository root:

```bash
./deploy/scripts/deploy.sh dev dev
```

For remote hosts:

```bash
./deploy/scripts/remote-deploy.sh dev dev user@dev-host
```

## containerd (nerdctl)

Containerd manifests remain available in `deploy/containerd/`, but operational install/deploy guidance is script-first via `deploy/scripts/*.sh`.

## Setup and Deployment Scripts

Make sure helper scripts are executable before first use:

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
```

Script entrypoints (all invoked from repo root):

- `./quickstart.sh`: interactive local bootstrap (preflight + `.env` + startup)
- `./deploy/scripts/preflight-check.sh`: host validation for required tools/files/permissions
- `./deploy/scripts/deploy.sh <dev|prod> <branch>`: deploy current repo on a host
- `./deploy/scripts/remote-deploy.sh <dev|prod> <branch> <user@host>`: deploy over SSH
- `./deploy/scripts/register-service.sh <name> <base-url> <etcd-url>`: register backend in etcd
- `./deploy/scripts/list-services.sh <etcd-url>`: inspect registered services
- `./deploy/scripts/backup-and-deploy-parallel.sh`: backup legacy host data (best-effort) and deploy Nexus on parallel ports


## Recommended Sequence

Local (single host):

1. `./quickstart.sh` (recommended)

Manual local alternative:

1. `./deploy/scripts/preflight-check.sh`
2. `cp .env.example .env` (edit as needed)
3. `docker compose up -d`

Parallel (side-by-side with an existing gateway/ai-infra deployment):

1. `./deploy/scripts/backup-and-deploy-parallel.sh` (recommended for first parallel cutover)

Remote host deploy:

1. Clone this repo to `/opt/nexus` on the remote host
2. Run `./deploy/scripts/remote-deploy.sh <dev|prod> <branch> <user@host>` from your local machine

## Windows development note

Nexus is deployed/operated from macOS/Linux hosts. If you develop on Windows, run all `deploy/scripts/*.sh` scripts from within WSL (Ubuntu) rather than PowerShell.

## Notes

- These manifests assume a shared `nexus` network for multi-host deployments.
- Update base URLs (e.g., `OLLAMA_BASE_URL`) to point to remote services when running across hosts.
- Persistence uses host bind mounts under `./.runtime/` (including gateway RO config at `./.runtime/gateway/config`).
- The UI is intentionally separated from the gateway for production deployments; keep it as a standalone container when it is implemented.
- For branch-based deploys, see `./deploy/scripts/deploy.sh` and `./deploy/scripts/remote-deploy.sh` (invoked from repo root).
- For etcd convenience, use `./deploy/scripts/register-service.sh` and `./deploy/scripts/list-services.sh` (invoked from repo root).
