# SDXL Turbo Service

Containerized FastAPI shim for SDXL Turbo exposing `POST /v1/images/generations` on Linux/NVIDIA hosts.

This is the Nexus replacement for the legacy `ai-infra/services/sdxl-turbo` systemd deployment.

## Runtime

- Recommended host: `ai1`
- Default port: `9050`
- GPU runtime: NVIDIA container runtime required

## Compose

Use [docker-compose.sdxl-turbo.yml](../../docker-compose.sdxl-turbo.yml).

## Key env vars

- `SDXL_TURBO_MODEL_ID`
- `SDXL_TURBO_CACHE_DIR`
- `SDXL_TURBO_DEVICE`
- `SDXL_TURBO_DTYPE`

## Gateway integration

Set `SDXL_TURBO_BASE_URL` in the gateway env to the reachable host URL for this service, for example `http://ai1:9050`.