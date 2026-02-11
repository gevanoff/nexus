# Images Service

OpenAI-compatible Images API service.

## Overview

This service is implemented as an **OpenAI Images API shim** (ported from `ai-infra/services/invokeai/shim`).

- Exposes `POST /v1/images/generations` returning `data[].b64_json`.
- Default mode is `SHIM_MODE=stub`, which returns a tiny PNG for contract testing.
- Optional mode `SHIM_MODE=invokeai_queue` can proxy to an InvokeAI instance (requires additional config).

## Status

✅ Implemented (shim; stub-by-default)

## Endpoints

- `GET /health` (always 200 if process is running)
- `GET /readyz`
  - In `stub` mode: always ready
  - In `invokeai_queue` mode: checks upstream InvokeAI
- `POST /v1/images/generations`
- `GET /v1/models` (best-effort; includes shim presets)
- `GET /v1/metadata`

## Configuration

See `env/images.env.example`.

Key env vars:

- `SHIM_MODE=stub|invokeai_queue`
- `INVOKEAI_BASE_URL=http://invokeai:9090`

## Quick test

```bash
curl -sS http://localhost:7860/health

curl -sS -X POST http://localhost:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"shim smoke test","response_format":"b64_json"}'
```

## Docker Compose (current)

Nexus persists images state on the host under `nexus/.runtime/images/` and bind-mounts it into the container.

```yaml
images:
  build:
    context: ./services/images
    dockerfile: Dockerfile
  ports:
    - "7860:7860"
  environment:
    - SHIM_MODE=${IMAGES_SHIM_MODE:-stub}
    - SHIM_PORT=7860
    - INVOKEAI_BASE_URL=${INVOKEAI_BASE_URL:-http://invokeai:9090}
  volumes:
    - ./.runtime/images/data:/data
    - ./.runtime/images/models:/data/models
```

## Notes

In the default `stub` mode, no GPU is required.

## Contributing

Want to implement this service? See:
- [Template Service](../template/README.md)
- [SERVICE_API_SPECIFICATION.md](../../SERVICE_API_SPECIFICATION.md)
- [InvokeAI Documentation](https://invoke-ai.github.io/InvokeAI/)

## References

- [OpenAI Images API](https://platform.openai.com/docs/api-reference/images)
- [InvokeAI](https://github.com/invoke-ai/InvokeAI)
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- [Automatic1111](https://github.com/AUTOMATIC1111/stable-diffusion-webui)
- [SDXL](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
