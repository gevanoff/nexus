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

## Editing the encrypted ai2 SOPS overlay

The tracked host-secret source is:

```text
deploy/secrets/prod/ai2.env.sops
```

Edit it only from a trusted machine that has the **existing** age private key.
Do not run `keygen` merely because the key is missing on the current machine:
a newly generated key cannot decrypt the existing encrypted file.

The expected key path is normally:

```text
~/.config/sops/age/keys.txt
```

Verify that the private-key file exists without printing it:

```bash
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
test -s "$SOPS_AGE_KEY_FILE" \
  && echo "age key present" \
  || echo "age key missing"
```

It is safe to print the corresponding public recipient:

```bash
grep '^# public key:' "$SOPS_AGE_KEY_FILE"
```

For the currently tracked `ai2.env.sops`, that output must include:

```text
age1qa48em2nrpd8g899lks345av6c99qle3s73jqrtmd536y8427ezs80ad9n
```

If the key is missing or the public recipient does not match, stop and use the
trusted control node that holds the original key, normally `copyfail`. Never
copy the private key into the repository.

From the Nexus checkout containing this PR branch:

```bash
git fetch origin
git switch agent/deployment-admin-control-plane 2>/dev/null \
  || git switch -c agent/deployment-admin-control-plane \
       --track origin/agent/deployment-admin-control-plane
git pull --ff-only
```

Open the encrypted file through the repository helper. `sops` decrypts the
values into the editor and re-encrypts the file when the editor saves:

```bash
EDITOR=nano ./deploy/scripts/sops-secrets.sh edit \
  --environment prod \
  --host ai2
```

Add or update these dotenv entries before the `sops_...` metadata block, if the
metadata is visible in the editor:

```dotenv
DEPLOY_CONTROL_GATEWAY_TOKEN=<contents of the controller token file on copyfail>
CLOUDFLARED_TUNNEL_TOKEN=<Cloudflare remotely managed tunnel token>
NEXUS_HOST_COPYFAIL_IP=<private copyfail IPv4 address reachable from ai2>
```

Do not paste any of those secret values into an issue, pull-request comment, or
terminal command line. On macOS, the controller token can be transferred to the
local clipboard without displaying it:

```bash
ssh ai@copyfail 'cat /data/nexus-runtime/deployment-control/token' | pbcopy
```

Likewise, an existing local Cloudflare token can be copied from `.env` without
printing it:

```bash
awk -F= '$1 == "CLOUDFLARED_TUNNEL_TOKEN" {sub(/^[^=]*=/, ""); printf "%s", $0}' \
  .env | pbcopy
```

To identify candidate private addresses on `copyfail`:

```bash
ssh ai@copyfail 'ip -4 -o addr show scope global'
```

Use the private address on the network routable from `ai2`, not a public address.
For Nano, save with `Ctrl-O`, press Enter, and exit with `Ctrl-X`.

Verify that the tracked file contains encrypted values, not plaintext:

```bash
grep -E \
  '^(DEPLOY_CONTROL_GATEWAY_TOKEN|CLOUDFLARED_TUNNEL_TOKEN|NEXUS_HOST_COPYFAIL_IP)=ENC\[' \
  deploy/secrets/prod/ai2.env.sops
```

Verify decryption without printing the values:

```bash
./deploy/scripts/sops-secrets.sh decrypt --environment prod --host ai2 \
  | awk -F= '/^(DEPLOY_CONTROL_GATEWAY_TOKEN|CLOUDFLARED_TUNNEL_TOKEN|NEXUS_HOST_COPYFAIL_IP)=/ {print $1 "=<present>"}'
```

Materialize the generated, git-ignored host overlay as a final check:

```bash
./deploy/scripts/sops-secrets.sh materialize \
  --environment prod \
  --topology-host ai2
```

Commit only the encrypted source file:

```bash
git add deploy/secrets/prod/ai2.env.sops
git commit -m "Provision deployment administration secrets"
git push origin agent/deployment-admin-control-plane
```

Never commit the generated `.sops.local` overlays, plaintext `.env` files, the
age private key, or the deployment-controller token file.

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
