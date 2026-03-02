# Qwen3-TTS Service

OpenAI-compatible Qwen3-TTS shim for Nexus (`POST /v1/audio/speech`).

## Status

🚧 Containerized shim ported from `ai-infra/services/qwen3-tts`.

## Endpoints

- `GET /health`
- `GET /readyz`
- `GET /v1/models`
- `POST /v1/audio/speech`
- `GET /v1/metadata`

## Configuration

- Env template: `env/qwen3-tts.env.example`
- Port: `9175`

Readiness behavior:

- `readyz` is healthy when either:
  - `QWEN3_TTS_UPSTREAM_BASE_URL` points to a reachable OpenAI-compatible TTS upstream, or
  - `QWEN3_TTS_RUN_COMMAND` is configured and produces audio output.

## Compose

Use component compose file:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml -f docker-compose.qwen3-tts.yml up -d --build
```
