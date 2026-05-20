# CI/CD and Branch-Based Deployments

This guide describes the current Nexus deployment standard and the optional registry-backed CI workflows included in this repository.

## Standard Deployment Flow

The standard Nexus path for tracked code changes is:

1. Commit the change from a development checkout.
2. Push the intended branch to `origin`.
3. Deploy that branch on the target host with `deploy/scripts/deploy.sh` or `deploy/scripts/remote-deploy.sh`.

This is the default operational path. Do not treat GitHub Actions image build/push workflows as the required deployment mechanism for ordinary Nexus updates.

## Registry-Backed CI Workflows

This repository also includes manual GitHub Actions workflows that build and push container images before running the remote deploy step. Those workflows are only valid when the registry secrets are configured.

## Secrets Management

- Store secrets in a **host-side env file** (recommended: `deploy/env/.env.dev` and `deploy/env/.env.prod`).
- Keep env files **out of git** and managed by host admins.
- For stronger isolation, use **Docker secrets** or a secrets manager (Vault, AWS Secrets Manager).
- Store CI secrets in GitHub Actions **Secrets** (registry credentials, SSH keys).

## GitHub Actions Workflows

This repository includes example workflows:

- `.github/workflows/build-and-deploy-dev.yml`
- `.github/workflows/build-and-deploy-prod.yml`

They require the following GitHub Secrets:

- `CONTAINER_REGISTRY`
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `DEV_SSH_HOST`, `DEV_SSH_USER`, `DEV_SSH_KEY`
- `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_KEY`

If those secrets are not configured, the workflows now fail immediately with an explicit message explaining that the standard Nexus deployment path is commit, push, and host deploy.

These workflows are for the optional registry-backed CI path only. They are not the source of truth for how Nexus is normally deployed.

## Convenience Scripts

Ensure script execute permissions:

```bash
chmod +x deploy/scripts/*.sh quickstart.sh
```

- `deploy/scripts/install-host-deps.sh`: interactive host dependency installer for Docker/Compose (+ optional NVIDIA runtime).
- `deploy/scripts/register-service.sh`: register a service in etcd.
- `deploy/scripts/list-services.sh`: list registered services from etcd.
- `deploy/scripts/migrate-from-ai-infra.sh`: interactive migration helper from ai-infra to Nexus.

## Dev Branch Deployment

- Create a permanent `dev` branch.
- Configure hosts to run dev containers with `docker-compose.<service>.dev.yml` overrides (e.g. `docker-compose.gateway.dev.yml`).
- Use separate ports, volumes, and network names to avoid collisions with production.

### Example: Deploy dev branch

```bash
./deploy/scripts/deploy.sh dev dev
```

By default, `deploy/scripts/deploy.sh` will use `deploy/env/.env.dev` if it exists, otherwise it falls back to `./.env`.
Create `deploy/env/.env.dev` by copying from `./.env.example` (see `deploy/env/README.md`).

### Remote deployment (from CI or operator machine)

```bash
./deploy/scripts/remote-deploy.sh dev dev user@dev-host
```

## Production Deployment

### Example: Deploy main branch

```bash
./deploy/scripts/deploy.sh prod main
```

By default, `deploy/scripts/deploy.sh` will use `deploy/env/.env.prod` if it exists, otherwise it falls back to `./.env`.
Create `deploy/env/.env.prod` by copying from `./.env.example` (see `deploy/env/README.md`).

## Notes

- The deploy scripts assume the host has docker compose installed.
- Nexus is operated on macOS/Linux hosts. If you develop on Windows, run deploy scripts and SSH from within WSL.
- A registry-backed CI flow is optional and only applies if you intentionally configure the required registry secrets.
- Gate production deploys behind manual approval and/or a protected branch policy.
