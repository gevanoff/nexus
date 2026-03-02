# LuxTTS Service

OpenAI-compatible LuxTTS shim for Nexus (`POST /v1/audio/speech`).

## Status

🚧 Containerized shim ported from `ai-infra/services/luxtts`.

## Endpoints

- `GET /health`
- `GET /readyz`
- `GET /v1/models`
- `POST /v1/audio/speech`
- `GET /v1/metadata`

## Configuration

- Env template: `env/luxtts.env.example`
- Port: `9170`
- Container-native default: baked-in `LUXTTS_RUN_COMMAND=python /app/app/scripts/run_luxtts.py`

Readiness behavior:

- `readyz` is healthy when either:
  - `LUXTTS_UPSTREAM_BASE_URL` points to a reachable OpenAI-compatible TTS upstream, or
  - `LUXTTS_RUN_COMMAND` is configured and produces audio output.

## Compose

Use component compose file:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml -f docker-compose.luxtts.yml up -d --build
```
