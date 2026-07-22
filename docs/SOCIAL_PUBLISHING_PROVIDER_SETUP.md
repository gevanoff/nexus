# Social Publishing Provider Setup

This document covers the Phase 2–4 provider implementation behind `/ui/social/publish`.

The implementation is intentionally brand-neutral. Brand profiles and generated drafts remain in the Phase 1 studio; account credentials, media, consent, and publication records are user-scoped and handled separately.

## Current implementation boundary

Included:

- encrypted OAuth token storage using a deployment-supplied Fernet key;
- expiring anti-forgery OAuth state records;
- Google/YouTube, Meta, and TikTok authorization-code flows;
- discovery of YouTube channels, Facebook Pages, linked Instagram professional accounts, and TikTok creators;
- local video ingestion with SHA-256 and `ffprobe` metadata;
- YouTube resumable upload initiation and upload;
- Facebook Page Reel upload and finish calls;
- Instagram Reel container creation through a temporary signed HTTPS media URL, followed by status polling and publication;
- TikTok creator-capability checks, explicit consent, chunk planning, upload, and status checks;
- provider-specific publication status records and idempotency keys.

Not included yet:

- durable background workers or scheduling (Phase 5);
- automatic retries after process restart;
- remote object-storage staging;
- thumbnails, playlists, Facebook scheduling UI, or provider analytics;
- app-review, verification, or audit submission on the user's behalf.

## Required common configuration

Generate a key once and store it in the deployment secret system:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set:

```dotenv
SOCIAL_PUBLISHING_ENABLED=true
SOCIAL_TOKEN_ENCRYPTION_KEY=<generated Fernet key>
SOCIAL_MEDIA_DIR=/var/lib/gateway/data/social_media
SOCIAL_MEDIA_MAX_BYTES=1073741824
SOCIAL_MEDIA_TTL_SEC=604800
SOCIAL_PROVIDER_HTTP_TIMEOUT_SEC=120
MAX_REQUEST_BYTES=1100000000
```

`SOCIAL_TOKEN_ENCRYPTION_KEY` must remain stable. Rotating it without a credential migration makes stored tokens unreadable and requires users to reconnect accounts.

Social publishing state is always stored in `USER_DB_PATH`. The social tables have foreign keys to `users(id)`, so `SOCIAL_PUBLISH_DB_PATH` is intentionally unsupported. The gateway data volume already persists both user and social state.

The default deployment supports a video asset up to 1 GiB. `MAX_REQUEST_BYTES` is slightly larger to allow for multipart framing. `docker-compose.gateway.yml` passes the setting through from the deployment environment, and the bundled Nginx proxy permits 1100 MiB while streaming `/ui/api/social/media` without request buffering.

A complete environment template is available at `deploy/env/social-publishing.example`.

For Instagram media-container publishing, configure a public HTTPS origin that routes to the gateway without redirecting the signed media path:

```dotenv
SOCIAL_PUBLIC_BASE_URL=https://nexus.example.com
```

The generated path is `/social-media/<asset-id>?expires=...&sig=...`. It is time-limited and does not expose the Nexus UI session.

## Phase 2: YouTube

Official references:

- OAuth for web server applications: https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps
- YouTube OAuth scopes: https://developers.google.com/youtube/v3/guides/authentication
- Resumable upload protocol: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- Processing status: https://developers.google.com/youtube/v3/guides/implementation/videos

Create a Google Cloud OAuth web application, enable YouTube Data API v3, configure its consent screen, and register the exact callback URI.

```dotenv
SOCIAL_GOOGLE_CLIENT_ID=...
SOCIAL_GOOGLE_CLIENT_SECRET=...
SOCIAL_GOOGLE_REDIRECT_URI=https://nexus.example.com/ui/social/oauth/youtube/callback
SOCIAL_GOOGLE_SCOPES=openid,email,profile,https://www.googleapis.com/auth/youtube.readonly,https://www.googleapis.com/auth/youtube.upload
```

The implementation requests offline access and persists refresh tokens server-side. It creates a resumable session, persists the session URI encrypted before sending the file, uploads the video, then exposes a status refresh action that reads `processingDetails.processingStatus`.

Current field mapping:

- `title`
- `description`
- `tags`
- `category_id`
- `privacy_status`
- `publish_at`
- `made_for_kids`

YouTube does not expose a distinct “create Short” API operation; the uploaded media and YouTube's classification determine whether it is presented as a Short.

## Phase 3: Facebook and Instagram

Official Meta documentation is linked from Meta's maintained Postman collections:

- Facebook Reels Publishing: https://www.postman.com/meta/facebook/folder/simabyk/reels-publishing
- Instagram API collection: https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api
- Instagram content publishing guide: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing

Create a Meta app, add Facebook Login for Business and the required Instagram products, register the exact callback URI, and request the permissions approved for the app's use case.

Pin a currently supported Graph API version explicitly rather than allowing an implicit provider default:

```dotenv
SOCIAL_META_APP_ID=...
SOCIAL_META_APP_SECRET=...
SOCIAL_META_REDIRECT_URI=https://nexus.example.com/ui/social/oauth/meta/callback
SOCIAL_META_API_VERSION=vXX.X
SOCIAL_META_SCOPES=pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish
```

The callback discovers manageable Pages and any linked Instagram professional account. Facebook and Instagram account records use their Page access token. Disconnecting one discovered Meta account only revokes that local record; it does not revoke the shared Meta authorization for every discovered account.

Facebook Reel flow:

1. Start a Reel upload session.
2. Upload the local file to the returned upload URL or `rupload.facebook.com`.
3. Finish the upload with `video_state` and metadata.
4. Query the video's status.

Instagram Reel flow:

1. Generate a signed, one-hour HTTPS media URL.
2. Create a `REELS` media container.
3. Query `status_code` until `FINISHED`.
4. Call `media_publish`.

Instagram publishing requires a professional account. With Facebook Login, it must be associated with a manageable Facebook Page.

## Phase 4: TikTok

Official references:

- Login Kit for Web: https://developers.tiktok.com/doc/login-kit-web
- User access-token management: https://developers.tiktok.com/doc/oauth-user-access-token-management
- Content Posting API: https://developers.tiktok.com/doc/content-posting-api-get-started
- Creator information: https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
- Direct Post: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- Media transfer and chunk rules: https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
- Content-sharing UX guidelines: https://developers.tiktok.com/doc/content-sharing-guidelines

Register a TikTok web app with Login Kit and Content Posting API, configure the exact HTTPS callback, and obtain approval for the requested scopes.

```dotenv
SOCIAL_TIKTOK_CLIENT_KEY=...
SOCIAL_TIKTOK_CLIENT_SECRET=...
SOCIAL_TIKTOK_REDIRECT_URI=https://nexus.example.com/ui/social/oauth/tiktok/callback
SOCIAL_TIKTOK_SCOPES=user.info.basic,video.publish
```

Before every direct-post attempt, Nexus queries current creator information and validates:

- the user-selected privacy level against `privacy_level_options`;
- comment, Duet, and Stitch availability;
- the video's probed duration against `max_video_post_duration_sec`;
- the caption against TikTok's UTF-16 length limit.

The UI deliberately provides no default privacy selection and leaves interaction permissions unchecked. It also requires the TikTok Music Usage Confirmation and a separate Nexus publication confirmation.

Uploads follow TikTok's current chunk restrictions: a whole-file upload up to 64 MiB, otherwise sequential chunks no larger than 64 MiB, with the remainder merged into the final chunk.

TikTok status handling distinguishes a completed public post from an inbox handoff:

- `PUBLISH_COMPLETE` becomes `PUBLISHED`.
- `SEND_TO_USER_INBOX` becomes `AWAITING_USER_ACTION` and remains refreshable because the creator still has to finish the post in TikTok.
- failure states become `FAILED_PERMANENT`; other states remain `PROCESSING`.

Unaudited TikTok clients are subject to TikTok's visibility and usage restrictions. Production use requires the relevant TikTok review/audit and must comply with its permitted product-use model.

## Activation sequence

1. Keep `USER_AUTH_ENABLED=true` and sign in with a Nexus user.
2. Generate and persist `SOCIAL_TOKEN_ENCRYPTION_KEY`.
3. Copy the common settings and selected provider credentials into the deployment environment file.
4. Rebuild Gateway so the `cryptography` dependency is installed.
5. Recreate Nginx when using the bundled TLS proxy so its upload limit and streaming route are active.
6. Open `/ui/social` for drafting or `/ui/social/publish` for account connection and publishing.
7. Start with a small private/unlisted test video before enabling public publication.

## Security and failure behavior

- Browser responses never include access tokens, refresh tokens, provider upload URLs, or signed media URLs.
- Provider upload/session URLs are encrypted before file transfer begins.
- OAuth state is single-use and expires after 15 minutes by default.
- Media paths are generated server-side and scoped by user ID.
- Public media delivery requires an HMAC signature and short expiration.
- Provider errors are stored as redacted structured diagnostics; credentials are not included.
- An idempotency key prevents the same user/account/request tuple from creating duplicate local publication attempts.
- `FAILED_RETRYABLE` and `FAILED_PERMANENT` remain distinct for the Phase 5 worker and retry UI.
