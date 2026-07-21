# Images Service

OpenAI-compatible Images API service.

## Overview

This service is implemented as an **OpenAI Images API shim** (ported from `ai-infra/services/invokeai/shim`).

- Exposes `POST /v1/images/generations` returning `data[].b64_json`.
- Exposes `POST /v1/images/edits` for reference-image workflows.
- Default mode is `SHIM_MODE=stub`, which returns a tiny PNG for contract testing.
- Optional mode `SHIM_MODE=invokeai_queue` proxies to an InvokeAI instance.
- Text-to-image requests can automatically select an InvokeAI workflow based on the selected model family.

## Status

✅ Implemented (shim; stub-by-default)

## Endpoints

- `GET /health` (always 200 if process is running)
- `GET /readyz`
  - In `stub` mode: always ready
  - In `invokeai_queue` mode: checks upstream InvokeAI
- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `GET /v1/models`
  - Lists base-generation models only.
  - Defaults to InvokeAI model type `main`.
  - Includes detected model family and workflow availability in metadata.
- `GET /v1/metadata`

## Configuration

Key environment variables:

- `SHIM_MODE=stub|invokeai_queue`
- `SHIM_GRAPH_TEMPLATE_PATH=/app/shim/graph_template.json`
  - Legacy/default workflow.
  - The shim detects its model family and adds it to the routing map automatically.
- `SHIM_GENERATION_WORKFLOWS_JSON`
  - Optional JSON object mapping model families to exported InvokeAI workflows.
- `SHIM_GENERATION_MODEL_TYPES=main`
  - Controls which InvokeAI model types are exposed as base image generators.
  - Keep the default unless a verified InvokeAI version uses another type for complete generation models.
- `IMAGES_SHIM_INVOKEAI_BASE_URL=http://invokeai:9090`
- `IMAGES_HTTP_BASE_URL=http://images:7860` for local compose or `http://<host>:7860` for multi-host gateway routing
- `SHIM_TIMEOUT_S=900` bounds the InvokeAI queue wait for a complete serial batch.
- `IMAGES_HTTP_TIMEOUT_SEC=1200` is the Gateway deadline and must remain larger
  than `SHIM_TIMEOUT_S`.

The Image UI assigns each request a progress id and polls the shim through the
Gateway. The shim subscribes to InvokeAI's `invocation_progress` Socket.IO
events, combines per-invocation progress with completed items in a batch, and
exposes the result at `GET /v1/images/progress/{progress_id}`. If the event
connection is unavailable, progress remains tied to actual queue state and
completed images; it does not synthesize a percentage.

### Family-specific workflow routing

A selected model is resolved against InvokeAI's model catalog. The shim reads the model's `base` metadata and chooses the matching workflow.

Supported family keys currently include:

- `sdxl`
- `flux`
- `flux2` for FLUX.2 Klein
- `z-image`
- `sd3`
- `sd` for Stable Diffusion 1.x/2.x

Example:

```env
SHIM_GRAPH_TEMPLATE_PATH=/app/shim/graph_template.json
SHIM_GENERATION_WORKFLOWS_JSON={"sd":"/app/shim/graph_template_sd.json","flux":"/app/shim/graph_template_flux.json","sd3":"/app/shim/graph_template_sd3.json","flux2":"/app/shim/graph_template_flux2.json","z-image":"/app/shim/graph_template_z_image.json"}
```

A map value can be either a path string or an object:

```json
{
  "flux": {
    "path": "/data/workflows/flux-text-to-image.json",
    "output_node_id": "final-image"
  }
}
```

`output_node_id` is optional when the shim can identify the final image-output node in the exported workflow.

The shim image includes text-to-image workflows for SD1.5/2, SDXL, SD3.5,
FLUX.1, FLUX.2 Klein, and Z-Image. Model UUIDs are deliberately absent from the
bundled workflows. At request time the shim inserts the selected main model and
resolves required auxiliary models from InvokeAI's catalog. This supports both
self-contained Diffusers models and quantized models that use separately
installed T5, CLIP, Qwen3, or VAE components.

Custom exports can still be placed in `${NEXUS_RUNTIME_ROOT}/images/workflows`
and mapped by family. This keeps InvokeAI model data under `/data/invokeai`
distinct from Nexus runtime-owned workflow configuration under
`/data/nexus-runtime/images/workflows`.

The workflows directory is mounted read-only inside the container:

```text
${NEXUS_RUNTIME_ROOT}/images/workflows -> /data/workflows
```

Place exported workflows there before enabling their family entries.

### Model dropdown behavior

The Image UI lists InvokeAI models whose type is `main`. This intentionally excludes auxiliary models such as:

- LoRA
- VAE
- ControlNet
- IP-Adapter
- T2I Adapter
- textual inversion / embeddings

Those assets participate through workflow-specific controls and should not be offered as base generation models.

Each dropdown label includes:

- human-readable model name
- model family
- InvokeAI UUID
- whether a matching workflow is configured

If an old UUID is submitted after a model was removed or re-imported, the shim returns an actionable error listing current installed base models.

## Important deployment notes

- The gateway and etcd `images` service record must point to the images shim on port `7860`.
- Raw InvokeAI on port `9090` is an upstream runtime for the shim and does not implement `POST /v1/images/generations`.
- If the UI is hitting `http://<host>:9090/v1/images/generations`, `IMAGES_HTTP_BASE_URL` or the `images` etcd record is wrong.
- In multi-host deployments, set `IMAGES_SHIM_INVOKEAI_BASE_URL` to the local upstream the shim should call, and keep `INVOKEAI_BASE_URL` / `IMAGES_ADVERTISE_BASE_URL` on the host-routable URLs the gateway should advertise.
- A supplied seed is applied to InvokeAI's random-integer node as the single-value interval `[seed, seed + 1)`, because its upper bound is exclusive.
- Multi-image seeded requests use consecutive seeds, so a batch is distinct but remains reproducible.
- Family-specific workflows must be genuine exports for that family. Changing only the model loader in an SDXL workflow does not turn it into a Flux, SD3, or SD 1.x workflow.

## Quick test

```bash
curl -sS http://localhost:7860/health

curl -sS http://localhost:7860/v1/models | jq .

curl -sS -X POST http://localhost:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"shim smoke test","model":"<invokeai-main-model-uuid>","response_format":"b64_json"}'
```

## Docker Compose

Nexus persists image state on the host and bind-mounts workflow exports into the container.

```yaml
images:
  environment:
    - SHIM_MODE=${IMAGES_SHIM_MODE:-stub}
    - SHIM_PORT=7860
    - SHIM_GRAPH_TEMPLATE_PATH=${SHIM_GRAPH_TEMPLATE_PATH:-/app/shim/graph_template.json}
    - SHIM_GENERATION_WORKFLOWS_JSON=${SHIM_GENERATION_WORKFLOWS_JSON:-}
    - SHIM_GENERATION_MODEL_TYPES=${SHIM_GENERATION_MODEL_TYPES:-main}
    - INVOKEAI_BASE_URL=${IMAGES_SHIM_INVOKEAI_BASE_URL:-${INVOKEAI_BASE_URL:-http://invokeai:9090}}
  volumes:
    - ${NEXUS_RUNTIME_ROOT}/images/data:/data
    - ${NEXUS_RUNTIME_ROOT}/images/models:/data/models
    - ${NEXUS_RUNTIME_ROOT}/images/workflows:/data/workflows:ro
```

## Image UI additions

The Gateway Image UI provides:

- a full-width prompt editor
- model labels with family and UUID context
- human-readable troubleshooting messages for nested image-backend failures
- an optional download naming scheme, such as `Cinder01`, which yields `Cinder01.png`, `Cinder02.png`, and so on

The naming scheme affects downloaded filenames; generated images remain stored using Nexus's content-addressed server-side filenames.

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
