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
- Container-native default: baked-in `LUXTTS_RUN_COMMAND=python /app/app/scripts/run_luxtts.py` (local runner)

Readiness behavior:

- `readyz` is healthy when either:
  - `LUXTTS_UPSTREAM_BASE_URL` points to a reachable OpenAI-compatible TTS upstream, or
  - `LUXTTS_RUN_COMMAND` is configured and local LuxTTS resources are available.

Startup refs sync behavior:

- By default, the container copies supported audio refs from shared mount `/var/lib/tts_refs` into local `/var/lib/luxtts/voices` at startup.
- Voice IDs are discovered from filenames in the local directory.
- This allows operators to rename files in `/var/lib/luxtts/voices` without changing shared source filenames.
- Sync controls:
  - `LUXTTS_SYNC_SHARED_REFS=true|false`
  - `LUXTTS_SYNC_OVERWRITE=true|false` (default `false`, preserves local edits/renames)

## Local-resource mode (no external inference service)

`LUXTTS_RUN_COMMAND` can run fully local if the LuxTTS code/resources exist under the mounted runtime path.

Expected runtime path inside container:

- `/var/lib/luxtts/app` (mapped from `./.runtime/luxtts/data` on host)

Required pieces:

- LuxTTS app/repo code (providing `zipvoice.luxvoice`)
- model access (via `LUXTTS_MODEL_ID`),
- prompt audio (`LUXTTS_PROMPT_AUDIO` or `LUXTTS_VOICE_MAP_JSON`).

Default prompt lookup now also checks `/var/lib/luxtts/voices/prompt.wav`.

## Compose

Use component compose file:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml -f docker-compose.luxtts.yml up -d --build
```
