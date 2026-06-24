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

- `TELEGRAM_TOKEN` (from @BotFather)
- `GATEWAY_BEARER_TOKEN` (same token Gateway expects)

Recommended defaults:

- `TELEGRAM_GATEWAY_BASE_URL=http://gateway:8800`
- `TELEGRAM_GATEWAY_MODEL=fast`
- `TELEGRAM_MAX_HISTORY=20`
- `TELEGRAM_MAX_MESSAGE=3900`
- `TELEGRAM_LOG_LEVEL=info`

The Telegram bot defaults to the `fast` alias rather than `auto` so direct chat traffic stays on the user-facing fast tier instead of the default MLX reasoning lane.

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
