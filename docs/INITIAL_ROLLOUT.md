# Initial Rollout Guide

This guide answers three practical questions:

1. How Nexus is initially rolled out.
2. What implicit requirements exist in the current code.
3. Whether a setup walkthrough script is necessary.

## 1) Initial Rollout Sequence

### Phase A: Single-host bootstrap

1. Clone the repository on a target host.
2. Run the preflight checker:
   ```bash
   ./deploy/scripts/preflight-check.sh
   ```
3. Run guided setup:
   ```bash
   ./quickstart.sh
   ```
4. Validate gateway and registry:
   ```bash
   curl http://localhost:8800/health
   curl -H "Authorization: Bearer <token>" http://localhost:8800/v1/registry
   ```

### Phase B: Multi-host rollout

1. Keep gateway + etcd on ingress host (or shared control host).
2. Start remote backend services using `deploy/docker-compose/*.yml` or `deploy/containerd/*.yml`.
3. Register remote backends in etcd:
   ```bash
   ./deploy/scripts/register-service.sh ollama http://ai1:11434 http://ai2:2379
   ```
4. Verify discovered services from gateway host.

### Phase C: Dev/prod branch deployment

1. Configure `dev` and `main` branch workflows.
2. Keep secrets in host files and CI secret stores (not in repo).
3. Use `deploy/scripts/deploy.sh` for environment-specific rollout.

## 2) Implicit Requirements in Current Code

### Runtime requirements

- Docker daemon + Docker Compose.
- `curl`, `openssl`, `python3` available on deployment hosts.
- etcd reachable from the gateway if `ETCD_ENABLED=true`.

### Repository requirements

- `services/gateway/Dockerfile` and `services/gateway/app/main.py` must exist.
- Optional `images` and `tts` Dockerfiles are currently required only for full profile startup.

### Security assumptions

- `.env` and `deploy/env/.env.*` should be permissions `600`.
- Gateway bearer token should be high entropy.
- Remote deployment via SSH assumes host key trust and locked-down credentials.

## 3) Is a Setup Walkthrough Script Necessary?

Yes. Despite the architecture being containerized, there are still practical host-level prerequisites and deployment footguns. The current implementation uses:

- `quickstart.sh`: interactive guided setup for first-time local rollout.
- `deploy/scripts/preflight-check.sh`: validates implicit dependencies and common misconfigurations.

These scripts reduce deployment variance and make failures easier to troubleshoot.

Before running them on fresh hosts, set execute permissions explicitly:

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
```
