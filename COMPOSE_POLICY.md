# Docker Compose policy (Nexus)

Policy: **one Docker Compose file per component**.

## Rationale

- Lets operators incrementally bring up/down specific components (`-f docker-compose.<component>.yml`) without editing a monolithic compose file.
- Keeps shared bind-mount paths explicit and discoverable via comments in each component file.
- Makes restart-loop troubleshooting easier by narrowing the blast radius to one container.

## Files

Base component files (production-ish defaults):

- `docker-compose.gateway.yml`
- `docker-compose.ollama.yml`
- `docker-compose.etcd.yml`
- `docker-compose.images.yml`
- `docker-compose.tts.yml`
- `docker-compose.luxtts.yml`
- `docker-compose.qwen3-tts.yml`
- `docker-compose.telegram-bot.yml`

## Usage

Core stack:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d
```


Add components:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.images.yml up -d
```

Core + Telegram bot:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.telegram-bot.yml up -d
```

