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
- `TELEGRAM_LOG_LEVEL=info`

The container health check verifies Telegram authentication, Gateway auth/model
mapping, and a minimal real chat completion. The bot also records the outcome of
each real chat request inside the container, so a recent user-visible Gateway
failure makes the Resources UI report that bot runtime as unhealthy instead of
leaving it green on cached backend readiness alone. The failure marker expires
after five minutes by default (`TELEGRAM_GATEWAY_FAILURE_MAX_AGE_MS`).

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

The default Telegram service uses the `ai2-chat` host alias. The optional host-bot services use their corresponding `ada2-chat` and `stackrot-chat` aliases.

## Host bot identities

The default service is the `ai2` bot. Two additional services are available under the `host-bots` profile:

| Service | Token | Model alias | SOUL.md |
| --- | --- | --- | --- |
| `telegram-bot` | `TELEGRAM_AI2_TOKEN` or legacy `TELEGRAM_TOKEN` | `ai2-chat` | `souls/ai2/SOUL.md` |
| `telegram-bot-ada2` | `TELEGRAM_ADA2_TOKEN` | `ada2-chat` | `souls/ada2/SOUL.md` |
| `telegram-bot-stackrot` | `TELEGRAM_STACKROT_TOKEN` | `stackrot-chat` | `souls/stackrot/SOUL.md` |

Each service requires a distinct BotFather token. Start the additional bots only after both tokens are configured:

```bash
docker compose --profile host-bots --env-file .env \
  -f docker-compose.gateway.yml -f docker-compose.telegram-bot.yml \
  up -d --build telegram-bot telegram-bot-ada2 telegram-bot-stackrot
```

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
