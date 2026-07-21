# Social Publishing Studio

## Objective

Add a Nexus-focused workflow that turns one factual video brief into editable, platform-specific publishing metadata and, in later phases, publishes the video through supported platform APIs.

The studio should reduce repetitive entry without flattening every destination into the same post. A user enters authoritative facts once, selects a brand profile, asks a Nexus language model for drafts, reviews the result, and keeps control over every field.

## Design principles

1. **Canonical facts, derived presentation.** The video brief is the source of truth. Titles, captions, descriptions, tags, links, and cover text are derived per platform.
2. **Generic rules with brand overlays.** Platform guidance is shared and brand-neutral. A selected brand may add vocabulary, voice, required facts, prohibited claims, calls to action, links, hashtags, and platform-specific guidance. Brand instructions must never leak into another brand.
3. **Human review before distribution.** Generated text is a draft. Phase 1 exposes the prompt and all generated fields for editing rather than treating model output as publication authority.
4. **Explicit contracts.** Each platform owns a documented field schema and validation rules. Unsupported data is surfaced rather than silently discarded.
5. **Isolated adapters and partial failure.** Future publishing integrations are separate adapters. A failure on one platform must not duplicate or invalidate successful publications elsewhere.
6. **User-scoped state.** Brand profiles and workspace state belong to the authenticated Nexus user.

## Phase 1: Drafting studio — started in this change

### Included

- Focused UI at `/ui/social`.
- Per-user brand profiles with:
  - name, description, audience, and voice;
  - preferred terminology and required facts;
  - prohibited or unsupported claims;
  - calls to action, default links, and default hashtags;
  - optional guidance for YouTube, Facebook, Instagram, and TikTok;
  - an optional brand-specific prompt addendum.
- A canonical video brief containing the subject, summary, transcript notes, key points, named people or organizations, dates or locations, goal, audience override, call to action, destination URL, factual constraints, and additional notes.
- Platform selection and Nexus model selection.
- Generic platform field contracts for:
  - YouTube: title, description, hashtags, tags, and thumbnail text;
  - Facebook: title, description, hashtags, link, and thumbnail text;
  - Instagram: caption, hashtags, alt text, and cover text;
  - TikTok: caption, hashtags, and cover text.
- Prompt preview showing the generic system instruction and the selected brand/video/platform context sent to the model.
- Structured draft generation through Nexus model routing.
- Normalization of model output into stable platform objects.
- Editable output, copy controls, and JSON export.
- Local-browser fallback when Nexus user authentication is disabled or unavailable.

### Deliberately excluded

- OAuth account connections.
- Video or thumbnail upload.
- Scheduling or direct publication.
- Platform analytics.
- Automatic use of trending topics, sounds, or unsupported claims.
- A built-in profile for any specific organization. Users create brands from the generic profile.

### Phase 1 acceptance criteria

- A user can create at least two brands with different guidance and switch between them without cross-contamination.
- A user can enter one video brief and obtain distinct drafts for any selected combination of the four platforms.
- The effective prompt can be inspected before generation.
- Brand guidance is applied only when that brand is selected.
- Every generated field remains editable and can be exported as JSON.
- Invalid or non-JSON model output produces a visible error rather than being treated as a valid draft.

## Phase 2: YouTube publishing

- Google OAuth connection and channel selection.
- Encrypted token storage and refresh handling.
- Resumable video uploads.
- Mapping for title, description, tags, category, playlist, audience, privacy, scheduling, and other supported fields.
- Processing-status polling and remote video ID persistence.
- Thumbnail handling according to YouTube and Shorts API limitations.
- Idempotency, audit logging, retries, and compliance/audit readiness.

## Phase 3: Facebook and Instagram publishing

- Meta OAuth and discovery of manageable Facebook Pages and associated Instagram Professional accounts.
- Secure media staging through temporary HTTPS URLs or object storage.
- Facebook Reel creation, upload, processing checks, and publication.
- Instagram media-container creation, processing checks, and publication.
- Platform-specific field validation, account capability checks, and API-version pinning.
- Remote publication IDs, retry safety, and account revocation handling.

## Phase 4: TikTok publishing

- TikTok OAuth and `video.publish` authorization.
- Creator-capability queries before presenting privacy, duet, stitch, comments, and duration controls.
- Direct file upload or verified-domain media transfer.
- Explicit user consent immediately before publication.
- Processing-status polling, remote post IDs, retry safety, and API audit requirements.

## Phase 5: Publication orchestration and scheduling

- Parent publication jobs with one child job per platform.
- States such as `DRAFT`, `READY_FOR_REVIEW`, `QUEUED`, `UPLOADING`, `PROCESSING`, `PUBLISHED`, `FAILED_RETRYABLE`, and `FAILED_PERMANENT`.
- Idempotency keys and stored remote IDs to prevent duplicate posts after ambiguous failures.
- Bounded retry/backoff policies and “retry failed platforms” controls.
- Scheduling, cancellation, account-specific time zones, and durable job recovery.
- Publication history and auditable request/response summaries without logging secrets.

## Phase 6: Analytics-assisted optimization

- Retrieve supported performance metrics per platform.
- Associate metrics with the original brief, brand, generated variant, edits, and publication.
- Generate brand-scoped observations such as effective title structures or caption lengths.
- Use those observations as optional prompt context without allowing one brand’s data to influence another.
- Support controlled variants and comparisons rather than autonomous optimization claims.

## Data model

### `BrandProfile`

User-managed guidance that applies across many videos. It contains brand identity and constraints, not facts about the current video.

### `VideoBrief`

The authoritative, reusable factual package for one video. Empty fields remain unknown; the model is instructed not to infer them.

### `PlatformDraft`

One editable rendering for a destination and variant. It stores the generated fields and can later record human edits separately.

### `ConnectedAccount` — future

Provider, external account ID, granted scopes, encrypted credentials, expiration, revocation, and capability state.

### `PublicationJob` — future

A parent job with platform child jobs, idempotency keys, status, attempts, remote IDs, and diagnostic summaries.

## Prompt architecture

Prompts are assembled in layers:

1. **Generic system instruction** — factuality, no invention, structured JSON, and separation of brand/video/platform concerns.
2. **Platform contracts** — requested fields and generic destination guidance.
3. **Selected brand profile** — terminology, voice, constraints, links, hashtags, and optional platform guidance.
4. **Current video brief** — authoritative facts and current call to action.
5. **Output schema** — exact JSON keys and requested number of variants.

The generic layer must not contain organization-specific terminology. Brand-specific instructions are visible in the prompt preview and included only for the selected profile.

## Security and operational requirements

- Never expose future OAuth refresh tokens to browser JavaScript.
- Encrypt provider credentials at rest and redact them from logs.
- Validate media type, dimensions, duration, and size before external upload.
- Pin provider API versions and retain capability/error diagnostics.
- Treat publication as a user-confirmed action.
- Preserve remote IDs and idempotency state before retrying uncertain operations.
- Keep analytics, prompts, and learned guidance scoped to the owning user and brand.
