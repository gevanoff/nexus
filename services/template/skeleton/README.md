# __SERVICE_TITLE__

Generated from the Nexus model service template.

This service exposes the standard Nexus shim contract for `__ROUTE_KIND__`.

## Defaults

- service name: `__SERVICE_NAME__`
- route kind: `__ROUTE_KIND__`
- port: `__PORT__`
- default model id: `__MODEL_ID__`

## Endpoints

- `GET /health`
- `GET /healthz`
- `GET /readyz`
- `GET /v1/models`
- `GET /v1/metadata`
- primary capability route derived from `NEXUS_ROUTE_KIND`

Route kind to path mapping:

- `chat` -> `POST /v1/chat/completions`
- `embeddings` -> `POST /v1/embeddings`
- `images` -> `POST /v1/images/generations`
- `tts` -> `POST /v1/audio/speech`
- `ocr` -> `POST /v1/ocr`
- `video` -> `POST /v1/videos/generations`
- `music` -> `POST /v1/music/generations`
- `json` -> `POST /v1/run`

## Configuration

Edit `.env.example` after scaffolding.

The most important settings are:

- `NEXUS_EXECUTION_MODE`
- `NEXUS_UPSTREAM_BASE_URL`
- `NEXUS_UPSTREAM_ENDPOINT`
- `NEXUS_RUN_COMMAND`
- `NEXUS_RUN_READY_COMMAND`

## Local Runner Contract

When `NEXUS_EXECUTION_MODE=command`, the shim exports:

- `NEXUS_JOB_ID`
- `NEXUS_ROUTE_KIND`
- `NEXUS_REQUEST_JSON`
- `NEXUS_OUTPUT_JSON`
- `NEXUS_OUTPUT_MEDIA_PATH`
- `NEXUS_OUTPUT_DIR`

Your runner should:

1. read the request JSON
2. perform inference
3. write a response JSON file
4. for `tts`, optionally write audio bytes and reference them from JSON

## Compose

Use the generated `docker-compose.__SERVICE_NAME__.yml` as the starting fragment.

It includes:

- the service container
- an etcd registrar sidecar
- a healthcheck
- a placeholder for GPU reservations

## Lifecycle Metadata

The generated `lifecycle.backend.json` is a paste-ready starter entry for `deploy/topology/backend_lifecycle.json`.

Before enabling the backend in production, fill in:

- host placement
- tier: `crucial`, `high`, or `optional`
- estimated idle VRAM and peak observed VRAM
- whether it can be auto-started or auto-stopped
- required secrets such as HF tokens
- required model artifact download steps
- whether readiness means process-ready, model-loaded, or externally reachable UI-ready
