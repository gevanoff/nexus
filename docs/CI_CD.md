# CI/CD and Branch-Based Deployments

This guide outlines a dev → main deployment workflow without exposing secrets in the repository.

## Automatic Build & Deploy (Suggested Flow)

1. **CI build**: build and tag images on pushes to `dev` and `main`.
2. **Artifact registry**: push images to a private registry (GHCR, ECR, GCR, etc.).
3. **Host deploy**: target hosts pull images and restart services using environment-specific config.

## Secrets Management

- Store secrets in a **host-side env file** (e.g., `deploy/env/.env.dev` and `.env.prod`).
- Keep env files **out of git** and managed by host admins.
- For stronger isolation, use **Docker secrets** or a secrets manager (Vault, AWS Secrets Manager).
- Store CI secrets in GitHub Actions **Secrets** (registry credentials, SSH keys).

## GitHub Actions Workflows

This repository includes example workflows:

- `.github/workflows/build-and-deploy-dev.yml`
- `.github/workflows/build-and-deploy-prod.yml`

They expect the following GitHub Secrets:

- `CONTAINER_REGISTRY`
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `DEV_SSH_HOST`, `DEV_SSH_USER`, `DEV_SSH_KEY`
- `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_KEY`

Update the workflows to build/push additional service images (ollama, images, tts) as those Dockerfiles are implemented.

## Convenience Scripts

Ensure script execute permissions:

```bash
chmod +x deploy/scripts/*.sh quickstart.sh
```

- `deploy/scripts/register-service.sh`: register a service in etcd.
- `deploy/scripts/list-services.sh`: list registered services from etcd.

## Dev Branch Deployment

- Create a permanent `dev` branch.
- Configure hosts to run dev containers with `docker-compose.dev.yml` overrides.
- Use separate ports, volumes, and network names to avoid collisions with production.

### Example: Deploy dev branch

```bash
./deploy/scripts/deploy.sh dev dev
```

### Remote deployment (from CI or operator machine)

```bash
./deploy/scripts/remote-deploy.sh dev dev user@dev-host
```

## Production Deployment

### Example: Deploy main branch

```bash
./deploy/scripts/deploy.sh prod main
```

## Notes

- The deploy scripts assume the host has docker compose installed.
- Use an external registry and `docker compose pull` if you want to avoid building on hosts.
- Gate production deploys behind manual approval and/or a protected branch policy.
