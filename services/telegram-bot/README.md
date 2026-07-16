# telegram-bot (Nexus)

Telegram bot bridge for Nexus Gateway.

## Role in Nexus

This service runs as its own container via `docker-compose.telegram-bot.yml` and forwards Telegram messages to Gateway OpenAI-style endpoints.

- Input: Telegram chats/commands
- Output: Gateway chat/image/speech/music requests
- Auth: `GATEWAY_BEARER_TOKEN`

## Linking a user chat for notifications

For proactive notifications, the bot needs a Telegram `chat_id`. A username by itself is not sufficient.

Use the Nexus UI to generate a one-time Telegram link command, then send that command to the bot in a private chat:

- Open `Settings -> Telegram`
- Click `Generate bot link command`
- Send the generated `/link ...` command to the bot in a direct message

The bot will bind that private chat to the signed-in Nexus user account so Gateway can deliver notifications there.

## Required configuration

Set these in `nexus/.env`:

- `TELEGRAM_AI2_TOKEN` (from @BotFather), or the legacy `TELEGRAM_TOKEN`
- `GATEWAY_BEARER_TOKEN` (same token Gateway expects)

Recommended defaults:

- `TELEGRAM_GATEWAY_BASE_URL=http://gateway:8800`
- `TELEGRAM_AI2_MODEL=ai2-chat`
- `TELEGRAM_MAX_HISTORY=20`
- `TELEGRAM_MAX_MESSAGE=3900`
- `TELEGRAM_MAX_TOKENS=512`
- `TELEGRAM_GATEWAY_TIMEOUT_MS=120000`
- `TELEGRAM_LOG_LEVEL=info`

Telegram chat completions use OpenAI SSE streaming internally, then send the
assembled answer to Telegram. Gateway keepalives prevent a queued or slow model
from looking like a dead network connection, while disconnects still cancel the
upstream stream. `TELEGRAM_MAX_TOKENS` bounds total generation and
`TELEGRAM_GATEWAY_TIMEOUT_MS` remains the maximum idle socket interval.

The container health check verifies Telegram authentication and Gateway
auth/model mapping. The bot also records the outcome of each real chat request
inside the container, so a recent user-visible Gateway failure makes the
Resources UI report that bot runtime as unhealthy instead of leaving it green
on cached backend readiness alone. The failure marker expires after five minutes
by default (`TELEGRAM_GATEWAY_FAILURE_MAX_AGE_MS`). A synthetic one-token
completion can be enabled with
`TELEGRAM_HEALTHCHECK_COMPLETION_ENABLED=true`, but it is off by default because
slow healthy models can exceed a short container-health timeout and frequent
synthetic inference consumes model capacity.

Transient Telegram DNS/connect failures are retried twice with incremental
backoff before the container health check fails. Tune this with
`TELEGRAM_HEALTHCHECK_NETWORK_RETRIES` and
`TELEGRAM_HEALTHCHECK_RETRY_DELAY_MS`. The Gateway Resources probe separately
retries transient `getMe` failures and keeps a bounded last-known-good result as
yellow/degraded for up to two failed status polls. Defaults are controlled by
`TELEGRAM_STATUS_PROBE_RETRIES=2`,
`TELEGRAM_STATUS_PROBE_RETRY_DELAY_SEC=0.25`,
`TELEGRAM_STATUS_FAILURE_THRESHOLD=3`, and
`TELEGRAM_STATUS_LAST_GOOD_MAX_AGE_SEC=300`. Authentication failures are never
masked by the last-known-good state.

## Shared Honcho memory

Set `TELEGRAM_MEMORY_ENABLED=true` only after the Gateway's authenticated Honcho
integration is configured. For each ordinary chat turn, the bot retrieves shared
long-term context before calling the model and records the completed user/assistant
turn afterward. Commands are not ingested. Honcho failures are logged and remain
non-fatal to the Telegram reply.

Private chats resolve through the immutable linked Nexus user and Telegram user
IDs. Groups remain isolated by numeric group chat ID. Long-term conclusions are
shared through the fleet observer, while each bot's immediate history remains
local and is never read by another bot. `TELEGRAM_MEMORY_TIMEOUT_MS` defaults to
10 seconds.

When memory is enabled, the container health check also requires the Gateway's
Honcho status endpoint to report enabled. This prevents a bot from appearing
healthy while its configured memory writes are being discarded.

The default Telegram service uses the `ai2-chat` host alias. The optional host-bot services use distinct identity aliases. Cinder currently uses the shared fast Devstral lane through `cinder-chat`; this keeps her SOUL and Honcho identity independent of the physical model placement.

## Host bot identities

The default service is the `ai2` bot. Three additional services are available under the `host-bots` profile:

| Service | Token | Model alias | SOUL.md |
| --- | --- | --- | --- |
| `telegram-bot` | `TELEGRAM_AI2_TOKEN` or legacy `TELEGRAM_TOKEN` | `ai2-chat` | `souls/ai2/SOUL.md` |
| `telegram-bot-ada2` | `TELEGRAM_ADA2_TOKEN` | `ada2-chat` | `souls/ada2/SOUL.md` |
| `telegram-bot-stackrot` | `TELEGRAM_STACKROT_TOKEN` | `stackrot-chat` | `souls/stackrot/SOUL.md` |
| `telegram-bot-meltdown` | `TELEGRAM_MELTDOWN_TOKEN` | `cinder-chat` | `souls/meltdown/SOUL.md` |

Because Cinder runs on a different Docker host from the Gateway, her service
defaults `GATEWAY_BASE_URL` to `http://ai2.embrient.com:8800` through
`TELEGRAM_MELTDOWN_GATEWAY_BASE_URL`. Do not use the Compose-local
`http://gateway:8800` hostname for the meltdown deployment.

Each service requires a distinct BotFather token. Start an additional bot only after its token is configured:

```bash
docker compose --profile host-bots --env-file .env \
  -f docker-compose.gateway.yml -f docker-compose.telegram-bot.yml \
  up -d --build telegram-bot telegram-bot-ada2 telegram-bot-stackrot telegram-bot-meltdown
```

Tokens may remain in each bot host's runtime `.env`; they do not need to be
duplicated into the ai2 Gateway environment. When the Gateway lacks a host bot's
token, the Resources UI trusts the remote container's healthy lifecycle state,
because the container health check already verifies Telegram authentication and
Gateway/model readiness.

`migraine` is intentionally absent from this Compose profile because its existing Hermes gateway already owns that Telegram identity and `~/.hermes/SOUL.md`.

## Group routing controls

The bot can be constrained in group chats so it does not respond to ambient group chatter or messages meant for other bots.

Optional `.env` settings:

```bash
# Comma-separated Telegram chat IDs. Empty means all chats are allowed.
TELEGRAM_ALLOWED_CHATS=-1003875008006

# Shared chats require addressing by default. Set false only for a dedicated room.
TELEGRAM_REQUIRE_MENTION=true

# When true, ignore messages containing @OtherBot mentions and commands like /help@OtherBot.
TELEGRAM_EXCLUSIVE_BOT_MENTIONS=true

# Comma-separated direct-address names or regexes. Literal entries are case-insensitive.
# Regex entries use the re: prefix.
TELEGRAM_MENTION_PATTERNS=Nexus,Hermes,re:\\boperator\\b
```

A group message is considered addressed to this bot when one of these is true:

- the message mentions the bot username, for example `@YourBot`
- the message directly addresses the bot's display name, such as `Nexus, answer this`
- the message is a reply to the bot
- the message directly addresses a literal `TELEGRAM_MENTION_PATTERNS` name, or matches an explicitly configured `re:` pattern
- the message is a bare slash command, such as `/help`

Messages and slash commands explicitly addressed to another bot are ignored before they reach the Gateway.

## Start / restart

From `nexus/`:

```bash
docker compose --env-file .env -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.telegram-bot.yml up -d --build
```

Restart only the bot:

```bash
docker compose --env-file .env -f docker-compose.gateway.yml -f docker-compose.telegram-bot.yml restart telegram-bot
```

## Logs

```bash
docker compose --env-file .env -f docker-compose.gateway.yml -f docker-compose.telegram-bot.yml logs -f telegram-bot
```

## Diagnostics

Use the Nexus diagnostic helper:

```bash
./deploy/scripts/diagnose-telegram-bot.sh
```

It validates:

- Effective `TELEGRAM_TOKEN` and `GATEWAY_BEARER_TOKEN`
- Telegram token validity via `getMe`
- Gateway reachability/auth
- Compose service visibility for `telegram-bot`

## Migration from ai-infra

If you previously ran the host-based bot in `ai-infra/services/telegram-bot`, copy these values into `nexus/.env`:

- `TELEGRAM_TOKEN`
- `GATEWAY_BEARER_TOKEN`

You do not need to migrate process managers (`systemd`/`launchd`) for Nexus container mode.
