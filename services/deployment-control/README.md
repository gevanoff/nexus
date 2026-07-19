# Deployment Control

Deployment Control is the single authenticated orchestration API for Nexus host
deployments. It runs on `copyfail`, where SOPS decryption and the dedicated
deployment SSH identity are centralized. Target hosts receive generated secret
overlays and do not need `sops` or an age private key.

The service serializes component-scoped deployments and invokes the existing
`deploy/scripts/remote-deploy.sh` workflow. It intentionally rejects empty
component lists, unknown topology hosts, unknown components, and branches that
are not explicitly allowlisted. It also rejects components that are not assigned
to the selected host in `deploy/topology/production.json`.

## Agent entry point

From the Nexus checkout in WSL:

```bash
./deploy/scripts/request-deploy.sh \
  --host ada2 \
  --component images \
  --reason "Deploy merged image workflow routing"
```

The wrapper connects to `copyfail`; the API token never leaves that host. Jobs
are queued, executed one at a time, and followed until completion by default.

## API

- `GET /health`
- `GET /v1/capabilities`
- `POST /v1/deployments`
- `GET /v1/deployments`
- `GET /v1/deployments/{job_id}`

All `/v1` routes require `Authorization: Bearer ...`.

## Host bootstrap

`copyfail` needs these protected files:

- `/home/ai/.config/sops/age/keys.txt`
- `/home/ai/.ssh/nexus-deployment-control`
- `/data/nexus-runtime/deployment-control/token`

The SSH public key must be authorized for the `ai` account on each managed
target. Keep private keys and the API token mode `0600`. The compose service
mounts only the dedicated private key, known-hosts file, age identity, and API
token read-only; it stores job state under
`/data/nexus-runtime/deployment-control`.

The controller sets `NEXUS_DEPLOY_SSH_IDENTITY_FILE` so
`remote-deploy.sh` uses only the dedicated identity. This avoids accidentally
depending on an interactive user's SSH agent or unrelated keys.

Before each job, the controller fast-forwards its checkout from the requested
allowlisted branch. Controller self-upgrades remain a bootstrap/recovery
operation because replacing the container that owns an active job would sever
its own executor. Use a component-scoped `remote-deploy.sh` from WSL only for
`deployment-control` itself.
