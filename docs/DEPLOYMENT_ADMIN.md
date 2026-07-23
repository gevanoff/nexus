# Nexus Deployment Administration

Nexus production deployments are executed by Deployment Control on `copyfail`.
The Gateway provides a separate administrator-facing API and UI; browsers never
contact the controller directly and never receive its bearer credential.

## Trust boundaries

```text
Administrator browser
  -> Nexus user session and admin check
  -> Gateway /ui/api/admin/deployments/*
  -> private Deployment Control API on copyfail
  -> topology-enforced remote-deploy.sh
  -> target host
```

Deployment Control remains responsible for serialization, branch and component
allowlists, topology enforcement, repository synchronization, execution logs,
and persisted job state. The Gateway is a narrowly scoped authenticated proxy.
It derives `requested_by` from the authenticated Nexus administrator rather than
accepting an arbitrary browser-provided identity.

## Gateway configuration on ai2

Provision these values through the generated SOPS overlay or an untracked local
overlay:

```dotenv
DEPLOY_CONTROL_BASE_URL=http://copyfail:9220
DEPLOY_CONTROL_GATEWAY_TOKEN=<strong service credential>
DEPLOY_CONTROL_TIMEOUT_SEC=20
NEXUS_DEPLOYMENT_TOPOLOGY_FILE=/workspace/nexus/deploy/topology/production.json
```

`DEPLOY_CONTROL_GATEWAY_TOKEN_FILE` may be used instead of the environment value
when the credential is mounted into the Gateway container. The browser never
receives either value.

## Controller listener on copyfail

The original command-line workflow binds Deployment Control to loopback because
`request-deploy.sh` executes its client on `copyfail` through SSH. The Gateway
proxy requires a private network listener:

```dotenv
DEPLOY_CONTROL_BIND_ADDRESS=0.0.0.0
DEPLOY_CONTROL_PORT=9220
```

Restrict TCP 9220 at the host firewall to `ai2` and trusted administrative
sources. The bearer token remains mandatory for every `/v1/*` controller route.
Do not publish this port through Cloudflare Tunnel or expose it to the Internet.

The current controller has one bearer credential. Until scoped controller
credentials are implemented, provision the same strong token to the Gateway
through SOPS. The operator SSH key and SOPS age private key remain only on
`copyfail`.

## Administrator UI

Open:

```text
https://nexus.shadowrepository.org/ui/admin/deployments
```

The page requires both Cloudflare Access and an authenticated Nexus user whose
`admin` flag is true. It provides:

- controller configuration and reachability status;
- allowed hosts, branches, and components;
- topology-aware component selection;
- deployment submission with an explicit confirmation;
- queued, running, succeeded, and failed job status;
- persisted controller log tails and failure details;
- automatic polling while deployments are active.

## Gateway API

All routes require an authenticated Nexus administrator session:

```text
GET  /ui/api/admin/deployments/status
GET  /ui/api/admin/deployments?limit=20
GET  /ui/api/admin/deployments/{job_id}
POST /ui/api/admin/deployments
```

Example request body:

```json
{
  "host": "ai2",
  "components": ["gateway", "cloudflared"],
  "branch": "main",
  "environment": "prod",
  "reason": "Deploy merged public UI changes"
}
```

The Gateway ignores any client-supplied actor and records the authenticated
administrator as `nexus-admin:<identity>`.

## Validation

From `services/gateway`:

```bash
python -m pytest -q tests/test_deployment_admin_routes.py
```

After configuring the private endpoint, verify from inside the Gateway
container:

```bash
docker exec nexus-gateway python - <<'PY'
import os, urllib.request
url = os.environ['DEPLOY_CONTROL_BASE_URL'].rstrip('/') + '/health'
print(urllib.request.urlopen(url, timeout=5).read().decode())
PY
```

Then open the Admin UI, confirm the controller is reachable, and submit a
component-scoped deployment to a non-critical service before using it for the
Gateway itself.
