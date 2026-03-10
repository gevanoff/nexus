# InvokeAI Runtime

Containerized InvokeAI runtime component for Linux/NVIDIA hosts.

Nexus uses the official InvokeAI container path documented by the upstream project, rather than a bespoke local build. The matching OpenAI-images shim remains the `images` component and can target this runtime with `INVOKEAI_BASE_URL=http://invokeai:9090`.

## Runtime

- Recommended host: `ada2`
- Default UI/API port: `9090`
- GPU runtime: NVIDIA container runtime required

## Compose

Use [docker-compose.invokeai.yml](../../docker-compose.invokeai.yml).

Nexus now treats raw InvokeAI as an operational upstream runtime, not as the direct gateway image backend.
The gateway-facing image backend remains the `images` shim, which should point to InvokeAI via `INVOKEAI_BASE_URL` when `SHIM_MODE=invokeai_queue`.

## Persistence

- Runtime root: `./.runtime/invokeai`
- Models, config, and outputs persist there via `/invokeai`

## Integration

Deploy both the InvokeAI runtime and the images shim on the same host for the current Nexus image stack:

```bash
./deploy/scripts/deploy.sh --components invokeai,images prod main
```

## Health Checks

For the raw InvokeAI runtime, the practical checks to verify are:

- container logs for startup/migration/model-load failures
- one of these version endpoints responding on port `9090`:
	- `/api/v1/app/version`
	- `/api/v1/version`
	- `/api/v1/app`
- GPU visibility inside the container
- write access and expected contents under `./.runtime/invokeai`

The compose healthcheck and the operational etcd registration use those same endpoint candidates.

Important routing split:

- InvokeAI runtime: `http://<host>:9090`
- Nexus images shim: `http://<host>:7860`

The UI/gateway should call the images shim, not raw InvokeAI.
If you see `405 Method Not Allowed` on `/v1/images/generations` against port `9090`, the gateway or etcd registration is pointed at the wrong service.