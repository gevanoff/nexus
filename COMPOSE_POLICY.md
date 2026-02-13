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

Dev overrides (optional, layer on top):

- `docker-compose.gateway.dev.yml`
- `docker-compose.ollama.dev.yml`
- `docker-compose.etcd.dev.yml`
- `docker-compose.images.dev.yml`
- `docker-compose.tts.dev.yml`

## Usage

Core stack:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d
```


Add components:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.images.yml up -d
```

Dev gateway:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.gateway.dev.yml up -d
```

Dev core stack:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.gateway.dev.yml \
  -f docker-compose.ollama.yml -f docker-compose.ollama.dev.yml \
  -f docker-compose.etcd.yml -f docker-compose.etcd.dev.yml up -d
```
