# InvokeAI Runtime

Containerized InvokeAI runtime component for Linux/NVIDIA hosts.

Nexus uses the official InvokeAI container path documented by the upstream project, rather than a bespoke local build. The matching OpenAI-images shim remains the `images` component and can target this runtime with `INVOKEAI_BASE_URL=http://invokeai:9090`.

## Runtime

- Recommended host: `ada2`
- Default UI/API port: `9090`
- GPU runtime: NVIDIA container runtime required

## Compose

Use [docker-compose.invokeai.yml](../../docker-compose.invokeai.yml).

## Persistence

- Runtime root: `./.runtime/invokeai`
- Models, config, and outputs persist there via `/invokeai`

## Integration

Deploy both the InvokeAI runtime and the images shim on the same host for the current Nexus image stack:

```bash
./deploy/scripts/deploy.sh --components invokeai,images prod main
```