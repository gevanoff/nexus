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
- Container-native default: baked-in `QWEN3_TTS_RUN_COMMAND=python /app/app/scripts/run_qwen3_tts.py` (local runner)

Readiness behavior:

- `readyz` is healthy when either:
  - `QWEN3_TTS_UPSTREAM_BASE_URL` points to a reachable OpenAI-compatible TTS upstream, or
  - `QWEN3_TTS_RUN_COMMAND` is configured and local Qwen3-TTS resources are available.

Startup refs sync behavior:

- By default, the container copies supported audio refs from shared mount `/var/lib/tts_refs` into local `/var/lib/qwen3-tts/voices` at startup.
- Voice IDs are discovered from filenames in the local directory.
- This allows operators to rename files in `/var/lib/qwen3-tts/voices` without changing shared source filenames.
- Sync controls:
  - `QWEN3_TTS_SYNC_SHARED_REFS=true|false`
  - `QWEN3_TTS_SYNC_OVERWRITE=true|false` (default `false`, preserves local edits/renames)

Voice exposure behavior:

- By default, `/v1/voices` exposes Qwen built-in voices (plus explicit `QWEN3_TTS_VOICES` / `QWEN3_TTS_VOICE_MAP_JSON` aliases).
- Ref-derived voice names are hidden by default to avoid presenting cross-model names that cannot be used as native Qwen speakers.
- To expose ref-derived names intentionally, set `QWEN3_TTS_EXPOSE_REF_VOICES=true`.

## Local-resource mode (no external inference service)

`QWEN3_TTS_RUN_COMMAND` can run fully local if the Qwen3-TTS code/resources exist under the mounted runtime path.

Expected runtime path inside container:

- `/var/lib/qwen3-tts/app` (mapped from `./.runtime/qwen3-tts/data` on host)

Required pieces:

- Qwen3-TTS app/repo code (providing `qwen_tts`)
- model access (via `QWEN3_TTS_MODEL_ID`),
- task-specific config (speaker/voice clone inputs).

## Compose

Use component compose file:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml -f docker-compose.qwen3-tts.yml up -d --build
```
