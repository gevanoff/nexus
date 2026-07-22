# Social Publishing Provider Setup

Nexus supports two distinct publishing workflows.

## Assisted publishing — default

Assisted publishing is always available through `/ui/social` and `/ui/social/publish`.
It requires no provider application, OAuth credentials, public callback URL, Cloudflare
Tunnel, or platform review.

The user:

1. creates and edits platform-specific drafts in `/ui/social`;
2. copies fields or exports the draft JSON;
3. opens YouTube Studio, Meta Business Suite, Instagram, or TikTok from
   `/ui/social/publish`;
4. uploads the local video and pastes the reviewed metadata; and
5. chooses privacy, scheduling, and final publication in the provider UI.

No feature flag controls this workflow. It is the normal baseline behavior of the
Social Publishing Studio.

## Direct API publishing — optional

The Phase 2–4 provider implementation adds OAuth account connections, encrypted
per-user tokens, media ingestion, provider uploads, and publication-status tracking.
It is intentionally disabled unless the deployment opts in:

```dotenv
SOCIAL_DIRECT_PUBLISHING_ENABLED=true
```

`SOCIAL_PUBLISHING_ENABLED` remains a deprecated compatibility alias. When both are
present, `SOCIAL_DIRECT_PUBLISHING_ENABLED` takes precedence.

Direct publishing still requires each provider's application configuration, approved
scopes, and any verification, review, or audit required by that provider. Enabling the
Nexus flag does not bypass those provider requirements.

## Common direct-publishing configuration

Generate a stable Fernet key and store it in the deployment secret system:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

```dotenv
SOCIAL_DIRECT_PUBLISHING_ENABLED=true
SOCIAL_TOKEN_ENCRYPTION_KEY=<generated Fernet key>
SOCIAL_MEDIA_DIR=/var/lib/gateway/data/social_media
SOCIAL_MEDIA_MAX_BYTES=1073741824
SOCIAL_MEDIA_TTL_SEC=604800
SOCIAL_PROVIDER_HTTP_TIMEOUT_SEC=120
SOCIAL_OAUTH_STATE_TTL_SEC=900
MAX_REQUEST_BYTES=1100000000
```

The encryption key must remain stable. Rotating it without a credential migration
makes existing user OAuth tokens unreadable and requires account reconnection.

Direct-publishing tables always share `USER_DB_PATH` because they reference
`users(id)`. `SOCIAL_PUBLISH_DB_PATH` is unsupported.

## Public HTTPS origin

Only direct Instagram publishing requires Meta to retrieve a temporary signed video
URL from Nexus:

```dotenv
SOCIAL_PUBLIC_BASE_URL=https://nexus.shadowrepository.org
```

The generated path is `/social-media/<asset-id>?expires=...&sig=...`. It is short-lived
and HMAC-signed. `docs/CLOUDFLARE_TUNNEL.md` documents one secure way to expose this
route without opening inbound origin ports.

Assisted publishing does not use this route and does not require a public Nexus URL.

## Google / YouTube direct publishing

Configure a Google OAuth web application, enable YouTube Data API v3, register the
exact callback, and obtain any required verification:

```dotenv
SOCIAL_GOOGLE_CLIENT_ID=...
SOCIAL_GOOGLE_CLIENT_SECRET=...
SOCIAL_GOOGLE_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/youtube/callback
SOCIAL_GOOGLE_SCOPES=openid,email,profile,https://www.googleapis.com/auth/youtube.readonly,https://www.googleapis.com/auth/youtube.upload
```

The adapter supports channel discovery, resumable upload, title, description, tags,
category, audience, privacy, optional scheduled publication, and processing-status
polling.

## Meta / Facebook and Instagram direct publishing

Configure a Meta application with Facebook Login for Business and the required
Facebook/Instagram products and permissions:

```dotenv
SOCIAL_META_APP_ID=...
SOCIAL_META_APP_SECRET=...
SOCIAL_META_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/meta/callback
SOCIAL_META_API_VERSION=vXX.X
SOCIAL_META_SCOPES=pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish
```

The adapter discovers manageable Facebook Pages and linked Instagram professional
accounts. Facebook Reels are uploaded directly. Instagram Reels use the temporary
signed HTTPS media URL described above.

## TikTok direct publishing

Configure a TikTok web application with Login Kit and Content Posting API, register
the callback, and obtain approval for the requested scopes:

```dotenv
SOCIAL_TIKTOK_CLIENT_KEY=...
SOCIAL_TIKTOK_CLIENT_SECRET=...
SOCIAL_TIKTOK_REDIRECT_URI=https://nexus.shadowrepository.org/ui/social/oauth/tiktok/callback
SOCIAL_TIKTOK_SCOPES=user.info.basic,video.publish
```

Before posting, Nexus queries creator capabilities and validates privacy, comments,
Duet, Stitch, duration, caption length, music usage confirmation, and explicit user
consent. `SEND_TO_USER_INBOX` remains `AWAITING_USER_ACTION`; only
`PUBLISH_COMPLETE` becomes `PUBLISHED`.

## Direct-publishing activation sequence

1. Keep `USER_AUTH_ENABLED=true` and sign into Nexus as a distinct user.
2. Set `SOCIAL_DIRECT_PUBLISHING_ENABLED=true`.
3. Generate and persist `SOCIAL_TOKEN_ENCRYPTION_KEY`.
4. Configure only the providers whose applications are ready.
5. Configure the public HTTPS origin only when direct Instagram publishing is needed.
6. Rebuild Gateway and recreate the relevant proxy/tunnel services.
7. Open `/ui/social/publish`; direct controls appear only when the direct flag is on.
8. Begin with private or unlisted test posts.

When the flag is off, the direct controls are hidden and the browser does not load
connected-account, media, or publication-status APIs. Assisted publishing remains
fully available.

## Security behavior

- Provider tokens and upload-session secrets are encrypted at rest.
- OAuth state is user-bound, single-use, and expiring.
- Account, media, and publication records are scoped to the authenticated Nexus user.
- Browser responses do not expose provider tokens or signed media URLs.
- Provider errors are stored as redacted structured diagnostics.
- Idempotency keys prevent duplicate local publication attempts.
