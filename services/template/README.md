# Nexus Model Service Template

This directory contains a real, reusable template for containerized Nexus model services.

The point of the template is to let an unfamiliar AI add a new model backend by following
one stable contract:

- same container shape
- same liveness and readiness endpoints
- same `GET /v1/models` discovery endpoint
- same modality route pattern
- same two execution modes
  - proxy to an upstream API
  - run a local command that reads a request JSON file and writes an output file

## What Can Be Standardized

Across the current model containers, these elements can and should be standardized:

1. Files
   - `Dockerfile`
   - `requirements.txt`
   - `.env.example`
   - `app/main.py`
   - `README.md`
   - optional `docker-compose.<service>.yml`

2. HTTP contract
   - `GET /health`
   - `GET /healthz`
   - `GET /readyz`
   - `GET /v1/models`
   - `GET /v1/metadata`
   - one capability route such as:
     - `/v1/chat/completions`
     - `/v1/embeddings`
     - `/v1/images/generations`
     - `/v1/audio/speech`
     - `/v1/ocr`
     - `/v1/videos/generations`
     - `/v1/music/generations`

3. Runtime modes
   - `upstream`: wrap an existing HTTP model server
   - `command`: execute a local inference runner

4. Local runner contract
   - request JSON path
   - output JSON path
   - output media path
   - output directory
   - job id and route kind

5. Compose shape
   - model service
   - optional etcd registrar sidecar
   - standard healthcheck
   - placeholder for GPU reservations

## Layout

The copyable service skeleton lives under [skeleton](/c:/Users/paper/Code/nexus/services/template/skeleton).

```text
services/template/
├── README.md
├── scaffold_service.py
└── skeleton/
    ├── .env.example
    ├── Dockerfile
    ├── README.md
    ├── docker-compose.service.yml
    ├── requirements.txt
    └── app/
        ├── __init__.py
        ├── main.py
        └── nexus_model_service.py
```

## Quick Start

Use the scaffolder instead of copying files by hand.

Directory: repo root

```bash
python services/template/scaffold_service.py \
  --name my-new-model \
  --route-kind images \
  --port 9190
```

That creates `services/my-new-model/` with:

- a runnable FastAPI shim
- an env template
- a compose fragment with a registrar sidecar
- a starter lifecycle-manager backend entry
- a README with the service defaults already filled in

## Execution Modes

### Upstream proxy mode

Use this when the model already exposes a compatible HTTP API.

Set:

- `NEXUS_EXECUTION_MODE=upstream`
- `NEXUS_UPSTREAM_BASE_URL=http://host:port`
- optionally `NEXUS_UPSTREAM_ENDPOINT`
- optionally `NEXUS_UPSTREAM_READY_PATHS`

### Local command mode

Use this when the model needs a custom runner script or CLI.

Set:

- `NEXUS_EXECUTION_MODE=command`
- `NEXUS_RUN_COMMAND=python app/run_model.py`
- optionally `NEXUS_RUN_READY_COMMAND=python app/check_assets.py`

For each request, the shim exports:

- `NEXUS_JOB_ID`
- `NEXUS_ROUTE_KIND`
- `NEXUS_REQUEST_JSON`
- `NEXUS_OUTPUT_JSON`
- `NEXUS_OUTPUT_MEDIA_PATH`
- `NEXUS_OUTPUT_DIR`

## Runner Output Contract

For `chat`, `embeddings`, `images`, `ocr`, `video`, `music`, and `json`, the
runner writes a JSON response to `NEXUS_OUTPUT_JSON`.

For `tts`, the runner can either:

1. write JSON containing `audio_path` or `audio_base64`
2. write raw bytes to `NEXUS_OUTPUT_MEDIA_PATH`

## When To Use This Template

Use the template when:

- the backend is request/response shaped
- the backend is already OpenAI-compatible or easy to wrap
- you want it to fit the Nexus health and registry model immediately

Fork a more specialized service instead when:

- the backend needs multipart uploads
- the backend requires substantial runtime provisioning at container start
- the backend needs long-lived worker orchestration that does not fit a simple runner

## Validation

Directory: repo root

```bash
python -m compileall services/template
python services/template/scaffold_service.py --name template-check --route-kind tts --port 9199 --dry-run
```

## Related Files

- [services/README.md](/c:/Users/paper/Code/nexus/services/README.md)
- [services/template/scaffold_service.py](/c:/Users/paper/Code/nexus/services/template/scaffold_service.py)
- [services/template/skeleton/app/nexus_model_service.py](/c:/Users/paper/Code/nexus/services/template/skeleton/app/nexus_model_service.py)
