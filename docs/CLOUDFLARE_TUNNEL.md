# Cloudflare Tunnel for Nexus

This deployment exposes Nexus at `https://nexus.shadowrepository.org` without assigning
Gateway a public IP or opening inbound firewall ports. A remotely managed
`cloudflared` connector runs beside Gateway on the `ai2` Docker host.

## When this is needed

Cloudflare Tunnel is optional.

It is useful when:

- Nexus should be reachable remotely through Cloudflare Access;
- OAuth providers need browser callback URLs for optional direct publishing; or
- direct Instagram publishing needs Meta to fetch a temporary signed media URL.

It is **not** required for assisted social publishing. The default assisted workflow
uses the Drafting Studio, local video files, copy/export controls, and each platform's
native uploader. That workflow needs no public Nexus hostname, provider credentials,
or platform review.

## Security model

- `cloudflared` makes outbound-only connections to Cloudflare.
- The connector publishes no host ports.
- Gateway and `cloudflared` share a dedicated internal Docker network.
- Gateway defaults to `172.29.0.2`; `cloudflared` defaults to `172.29.0.3`.
- Only the connector address is appended to `UI_IP_ALLOWLIST`.
- The tunnel token is mounted from a protected runtime file, not exposed through the container environment or process arguments.
- Cloudflare Access protects the UI and OAuth callbacks.
- Only `/social-media/*` bypasses Access, and those URLs remain short-lived and HMAC-signed by Nexus.
- Nexus authentication remains enabled behind Cloudflare Access.

Do not add the tunnel network to `UI_TRUST_PROXY_CIDRS`. Gateway should authorize the
fixed connector address rather than trust arbitrary forwarded Internet addresses.

## 1. Create the remotely managed tunnel

In Cloudflare Zero Trust:

1. Go to **Networks > Tunnels**.
2. Create a tunnel named `nexus-ai2`.
3. Choose the Docker connector instructions.
4. Copy only the long `eyJ...` tunnel token.
5. Do not run Cloudflare's generated `docker run` command.
6. Store the token in the protected `ai2` deployment environment or SOPS overlay:

```dotenv
CLOUDFLARED_TUNNEL_TOKEN=<tunnel-token>
CLOUDFLARED_IMAGE_TAG=2026.7.2
CLOUDFLARED_PROTOCOL=auto
```

The deployment script copies the token into
`${NEXUS_RUNTIME_ROOT}/cloudflared/tunnel-token`, inside a protected directory, and
mounts only that file read-only into the connector. Rotate the tunnel token in
Cloudflare if it is disclosed.

## 2. Configure the public hostname

In the tunnel's **Public Hostnames** configuration, add:

| Setting | Value |
|---|---|
| Subdomain | `nexus` |
| Domain | `shadowrepository.org` |
| Path | empty |
| Service type | `HTTP` |
| URL | `nexus-gateway-tunnel:8800` |

The service name resolves only inside the dedicated Docker tunnel network. Do not point
the tunnel at a host-published Gateway port.

## 3. Protect Nexus with Cloudflare Access

Create a self-hosted Access application for:

```text
nexus.shadowrepository.org
```

Add an **Allow** policy limited to approved users. An exact email allowlist is a good
initial policy.

Only when direct Instagram publishing is enabled, create a second, more-specific
self-hosted application for:

```text
nexus.shadowrepository.org/social-media/*
```

Give that path a **Bypass / Everyone** policy so Meta can retrieve signed media. Do not
bypass `/ui/*`, `/v1/*`, OAuth callbacks, or the entire hostname. Unsigned or expired
media requests are still rejected by Nexus.

When direct Instagram publishing is not enabled, omit this Bypass application.

## 4. Optional direct-publishing URLs

These values are needed only for configured direct provider adapters:

```dotenv
SOCIAL_PUBLIC_BASE_URL=https://nexus.shadowrepository.org
SOCIAL_GOOGLE_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/youtube/callback
SOCIAL_META_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/meta/callback
SOCIAL_TIKTOK_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/tiktok/callback
```

`SOCIAL_PUBLIC_BASE_URL` is specifically required by direct Instagram publishing.
Provider callback URLs are needed only for providers whose OAuth applications are
configured. Assisted publishing uses none of these values.

The dedicated network defaults are:

```dotenv
CLOUDFLARED_ORIGIN_SUBNET=172.29.0.0/29
CLOUDFLARED_GATEWAY_IP=172.29.0.2
CLOUDFLARED_CONNECTOR_IP=172.29.0.3
```

Change all three together if the subnet overlaps an existing Docker, LAN, VPN, or
overlay network.

## 5. Deploy

The `ai2` deployment environment is normally `deploy/env/.env.prod.ai2`:

```bash
bash deploy/scripts/deploy-cloudflared.sh \
  --env-file deploy/env/.env.prod.ai2
```

The script validates configuration, materializes the protected token file, pulls the
pinned image, ensures etcd, recreates Gateway with the tunnel attachment, starts
`nexus-cloudflared`, and waits for Gateway health.

Normal Gateway restarts do not need to recreate cloudflared. Re-run the script when
changing the tunnel token, version, subnet, or Gateway tunnel attachment.

## 6. Verify

```bash
docker ps --filter name=nexus-cloudflared
docker logs --tail=100 nexus-cloudflared
curl -I https://nexus.shadowrepository.org/ui/social/publish
```

The tunnel should be **Healthy** in Cloudflare Zero Trust. An unauthenticated UI
request should be redirected to or rejected by Cloudflare Access.

When the signed media Bypass application exists, this request should reach Nexus rather
than an Access login page:

```bash
curl -i https://nexus.shadowrepository.org/social-media/not-a-real-id
```

A real media request still requires valid `expires` and `sig` parameters.

## Operations

Rotate the token in Cloudflare, replace `CLOUDFLARED_TUNNEL_TOKEN`, and rerun the
deployment script. Upgrade `CLOUDFLARED_IMAGE_TAG` only after reviewing the release.
Stopping `nexus-cloudflared` removes public reachability without affecting private/LAN
Gateway access.

## Official references

- Cloudflare Tunnel overview: https://developers.cloudflare.com/tunnel/
- Tunnel setup: https://developers.cloudflare.com/tunnel/setup/
- Tunnel tokens: https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/
- Tunnel run parameters: https://developers.cloudflare.com/tunnel/advanced/run-parameters/
- Access application paths: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/
- Access policies and Bypass: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/
