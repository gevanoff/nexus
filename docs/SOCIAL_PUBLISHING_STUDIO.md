# Social Publishing Studio

## Objective

Turn one factual video brief into editable, platform-specific publishing metadata and
help the user publish it with the least friction.

The normal workflow is assisted publishing: Nexus prepares the material and the user
completes the upload in each platform's native interface. Optional provider adapters
can add direct API publication later without changing the baseline studio behavior.

## Design principles

1. **Canonical facts, derived presentation.** The video brief is the source of truth.
2. **Generic rules with brand overlays.** Brand guidance must not leak across users or brands.
3. **Human review before distribution.** Generated text is always an editable draft.
4. **Assisted publishing by default.** Copy, export, and native-uploader handoff require no provider credentials or review.
5. **Direct publishing is opt-in.** OAuth and provider API controls appear only when the deployment explicitly enables them.
6. **Explicit contracts.** Each platform owns a documented field schema and validation rules.
7. **User-scoped state.** Profiles, credentials, media, and publication records belong to the authenticated Nexus user.

## Assisted publishing — implemented and always available

### Drafting Studio

The focused UI at `/ui/social` provides:

- per-user brand profiles;
- a canonical video brief;
- platform selection for YouTube, Facebook, Instagram, and TikTok;
- Nexus model selection and prompt preview;
- structured, platform-specific draft generation;
- editable titles, captions, descriptions, tags, hashtags, links, alt text, and cover text;
- field-level copy controls; and
- JSON export.

The Drafting Studio has a local-browser fallback when Nexus user authentication is
unavailable.

### Native uploader handoff

`/ui/social/publish` always presents an assisted workflow:

1. prepare and review drafts in `/ui/social`;
2. copy fields or export JSON;
3. keep the finished video file locally;
4. open YouTube Studio, Meta Business Suite, Instagram, or TikTok; and
5. select privacy, scheduling, and final publication in the provider interface.

This path requires no:

- Google, Meta, or TikTok developer application;
- OAuth client ID or client secret;
- provider access or refresh token;
- provider application review;
- public Nexus callback URL;
- Cloudflare Tunnel; or
- social-publishing feature flag.

## Optional direct API publishing — implementation added

Direct publishing is hidden unless:

```dotenv
SOCIAL_DIRECT_PUBLISHING_ENABLED=true
```

The old `SOCIAL_PUBLISHING_ENABLED` variable remains a deprecated compatibility
fallback. The direct setting does not control assisted publishing.

### YouTube adapter

- Google OAuth connection and channel selection.
- Encrypted token storage and refresh handling.
- Resumable video uploads.
- Title, description, tags, category, audience, privacy, and scheduling mappings.
- Processing-status polling and remote video ID persistence.

Deployment still requires a Google application, consent-screen setup, scopes, and any
required verification.

### Facebook and Instagram adapters

- Meta OAuth and discovery of manageable Facebook Pages and linked Instagram professional accounts.
- Facebook Reel creation, upload, finish, and processing checks.
- Instagram media-container creation, processing checks, and publication.
- Temporary signed HTTPS media delivery for Instagram ingestion.
- Explicit Graph API version configuration.

Deployment still requires a configured Meta application, approved permissions, and a
public HTTPS origin for direct Instagram media retrieval.

### TikTok adapter

- TikTok OAuth and `video.publish` authorization.
- Creator-capability checks for privacy, comments, Duet, Stitch, and duration.
- Provider-compliant chunked upload.
- Explicit publication consent and Music Usage Confirmation.
- Status polling and remote publish ID persistence.

Deployment still requires TikTok application configuration, approved scopes, and the
applicable review or audit.

Exact optional provider configuration is documented in
`docs/SOCIAL_PUBLISHING_PROVIDER_SETUP.md`.

## Interface behavior

When direct publishing is disabled:

- assisted workflow and native uploader links remain visible;
- provider configuration cards are hidden;
- connected-account controls are hidden;
- Nexus media upload controls are hidden;
- direct publication controls and status history are hidden; and
- the browser does not load direct account, media, or publication APIs.

When direct publishing is enabled, those controls appear and provider readiness is
reported independently for YouTube, Meta, and TikTok.

## Data model

### `BrandProfile`

User-managed guidance that applies across videos and contains brand identity,
terminology, voice, constraints, links, hashtags, and platform guidance.

### `VideoBrief`

The authoritative factual package for one video. Empty fields remain unknown; the
model is instructed not to invent them.

### `PlatformDraft`

One editable platform rendering. It is useful in both assisted and direct workflows.

### `ConnectedAccount`

Optional direct-publishing record containing provider identity, granted scopes,
encrypted credentials, expiration, revocation, account type, and provider metadata.
Records are user-scoped.

### `PublicationJob`

Optional direct-publishing record containing the idempotency key, provider state,
remote ID, consent time, redacted response data, and structured errors.

## Future orchestration

A later phase may add:

- publication packages containing platform-specific media and metadata files;
- manual completion tracking and published URL recording;
- durable direct-publishing workers and retry policies;
- scheduling and cancellation;
- analytics-assisted optimization; and
- remote object-storage staging for direct Instagram publication.

These enhancements must preserve the always-available assisted path and must not make
provider credentials a prerequisite for drafting, export, or native uploader handoff.

## Security requirements

- Never expose provider tokens, upload-session URLs, or signed media URLs to browser JavaScript.
- Encrypt optional provider credentials and session secrets at rest.
- Validate media before external upload.
- Pin provider API versions where applicable.
- Require explicit user consent for direct publication.
- Preserve remote IDs and idempotency state before retrying uncertain operations.
- Keep prompts, credentials, media, and learned guidance scoped to the owning user and brand.
