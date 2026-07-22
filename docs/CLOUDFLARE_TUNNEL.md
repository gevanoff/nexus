# Cloudflare Tunnel for Nexus

This deployment exposes Nexus at `https://nexus.shadowrepository.org` without assigning the Gateway a public IP or opening inbound firewall ports. A remotely managed `cloudflared` connector runs beside Gateway on the `ai2` Docker host.

## Security model

- `cloudflared` makes outbound-only connections to Cloudflare.
- The connector publishes no host ports.
- Gateway and `cloudflared` share a dedicated internal Docker network.
- Gateway defaults to `172.29.0.2`; `cloudflared` defaults to `172.29.0.3`. The subnet and addresses are configurable together.
- The compose overlay appends only the connector address to `UI_IP_ALLOWLIST`.
- The tunnel token is mounted from a runtime token file rather than exposed through container environment variables or process arguments.
- Cloudflare Access protects the UI and OAuth callbacks.
- Only `/social-media/*` bypasses Access; those URLs are already short-lived and HMAC-signed by Nexus.
- Nexus user authentication remains enabled behind Cloudflare Access.

Do not add the tunnel network to `UI_TRUST_PROXY_CIDRS`. Gateway should authorize the fixed connector address rather than treating arbitrary Internet client addresses as UI-allowlisted proxies.

## 1. Create the remotely managed tunnel

In Cloudflare Zero Trust:

1. Go to **Networks > Tunnels**.
2. Create a Cloudflare Tunnel named `nexus-ai2`.
3. Choose the Docker connector instructions.
4. Copy only the tunnel token (the long `eyJ...` value). Do not run the generated `docker run` command.
5. Store the token in the protected ai2 deployment environment or SOPS overlay:

```dotenv
CLOUDFLARED_TUNNEL_TOKEN=<tunnel-token>
CLOUDFLARED_IMAGE_TAG=2026.7.2
CLOUDFLARED_PROTOCOL=auto
```

The deployment script copies the token into `${NEXUS_RUNTIME_ROOT}/cloudflared/tunnel-token`, inside a mode-`0700` directory, and mounts only that file read-only into the connector. The token is removed from the script environment before Compose starts the containers.

The token authorizes a connector to run this one tunnel. Rotate it in Cloudflare if it is disclosed.

## 2. Configure the public hostname

In the tunnel's **Public Hostnames** configuration, add:

| Setting | Value |
|---|---|
| Subdomain | `nexus` |
| Domain | `shadowrepository.org` |
| Path | empty |
| Service type | `HTTP` |
| URL | `nexus-gateway-tunnel:8800` |

The service name resolves only inside the dedicated Docker tunnel network. Do not point the tunnel at a host-published Gateway port.

Cloudflare will create or associate the proxied DNS route for `nexus.shadowrepository.org` with the tunnel.

## 3. Protect Nexus with Cloudflare Access

Create a self-hosted Access application for:

```text
nexus.shadowrepository.org
```

Add an **Allow** policy limited to the people who should use Nexus. An exact email allowlist is preferable for the initial deployment. OAuth flows should be started from the tunneled Nexus URL so the browser already has both its Cloudflare Access session and Nexus login session when the provider redirects back.

Create a second, more-specific self-hosted Access application for:

```text
nexus.shadowrepository.org/social-media/*
```

Give that path a **Bypass / Everyone** policy. The more-specific path application takes precedence over the hostname-wide application. Do not bypass `/ui/*`, `/v1/*`, OAuth callback paths, or the whole hostname.

The public media route remains protected by Nexus-generated expiration and HMAC query parameters. An unsigned or expired request is rejected by Gateway.

## 4. Configure Nexus provider URLs

Copy the relevant values from `deploy/env/cloudflared.example` and `deploy/env/social-publishing.example` into the ai2 deployment environment:

```dotenv
SOCIAL_PUBLIC_BASE_URL=https://nexus.shadowrepository.org
SOCIAL_GOOGLE_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/youtube/callback
SOCIAL_META_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/meta/callback
SOCIAL_TIKTOK_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/tiktok/callback
```

Register those exact callback URLs in the corresponding Google, Meta, and TikTok developer applications.

The dedicated network defaults are:

```dotenv
CLOUDFLARED_ORIGIN_SUBNET=172.29.0.0/29
CLOUDFLARED_GATEWAY_IP=172.29.0.2
CLOUDFLARED_CONNECTOR_IP=172.29.0.3
```

Change all three together before deployment if the default subnet overlaps an existing Docker, LAN, VPN, or overlay network.

## 5. Deploy

The ai2 topology deployment environment is normally `deploy/env/.env.prod.ai2`. Deploy with:

```bash
bash deploy/scripts/deploy-cloudflared.sh \
  --env-file deploy/env/.env.prod.ai2
```

The script:

1. verifies that the tunnel token exists;
2. writes the protected runtime token file;
3. validates the combined Gateway, etcd, and cloudflared Compose configuration;
4. pulls the pinned cloudflared image;
5. ensures etcd is running;
6. recreates Gateway so it joins the tunnel network and receives the connector allowlist entry;
7. starts `nexus-cloudflared`; and
8. waits for Gateway health.

Normal Gateway restarts do not need to recreate cloudflared. Re-run this deployment script when changing the tunnel network, token, cloudflared version, or Gateway tunnel attachment.

## 6. Verify

Check the local connector:

```bash
docker ps --filter name=nexus-cloudflared
docker logs --tail=100 nexus-cloudflared
```

The tunnel should appear **Healthy** in Cloudflare Zero Trust.

Test Access protection:

```bash
curl -I https://nexus.shadowrepository.org/ui/social/publish
```

An unauthenticated request should be redirected to or rejected by Cloudflare Access.

Test the narrowly public media path:

```bash
curl -i https://nexus.shadowrepository.org/social-media/not-a-real-id
```

This request should reach Gateway and return a Nexus/FastAPI error rather than a Cloudflare Access login response. A real media URL still requires valid `expires` and `sig` query parameters.

Finally, open this URL in a browser, complete Cloudflare Access authentication, then sign into Nexus:

```text
https://nexus.shadowrepository.org/ui/social/publish
```

## Operations

### Rotate the tunnel token

1. Rotate the token in Cloudflare Zero Trust.
2. Replace `CLOUDFLARED_TUNNEL_TOKEN` in the protected deployment environment.
3. Re-run `deploy-cloudflared.sh`.

### Upgrade cloudflared

Change `CLOUDFLARED_IMAGE_TAG` only after reviewing the Cloudflare release. The deployment script pulls the selected image before recreating the connector.

### Stop the connector

```bash
docker stop nexus-cloudflared
```

Stopping the connector removes public reachability but does not affect private/LAN Gateway access.

## Official references

- Cloudflare Tunnel overview: https://developers.cloudflare.com/tunnel/
- Tunnel setup: https://developers.cloudflare.com/tunnel/setup/
- Tunnel tokens: https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/
- Tunnel run parameters: https://developers.cloudflare.com/tunnel/advanced/run-parameters/
- Cloudflare Access application paths: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/
- Cloudflare Access policies and Bypass: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/
