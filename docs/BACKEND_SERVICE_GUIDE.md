# Backend Service Bring-Up Guide

This note is for agents adding or trading in Nexus model backends.

## Current Contract

Every backend should have the same operational shape:

- `GET /health` or `/healthz`: process liveness only.
- `GET /readyz`: returns 200 only when the service can handle a real request.
- `GET /v1/models`: returns the model id the gateway can display.
- One capability route, such as `/v1/ocr`, `/v1/music/generations`, or `/v1/videos/generations`.
- An etcd registrar sidecar with the right backend class and advertised base URL.
- A lifecycle-manager entry in `deploy/topology/backend_lifecycle.json`.

Use `services/template/scaffold_service.py` for new request/response backends. It now emits a starter `lifecycle.backend.json` entry as well as the FastAPI shim and compose fragment.

## Priority Tiers

Use lifecycle tiers deliberately:

- `crucial`: control plane, default chat, embeddings, or services that should not be stopped automatically.
- `high`: important defaults that may be interrupted only by an explicit operator action.
- `optional`: specialized or heavy backends that should trade in and out based on user requests.

Set both idle and peak memory expectations. For bursty media services, peak memory matters more than the idle process footprint.

## Placement Rules

Choose the host from the backend's acceleration path:

- CUDA/PyTorch and NVIDIA-specific runtimes belong on Linux/NVIDIA hosts.
- MLX or Apple Silicon paths belong host-native on `ai2`.
- CPU-only services belong on `ai2` unless they need proximity to GPU artifacts or media volumes.

Do not put a new persistent CUDA model on `ada2` without accounting for `vllm-strong`, InvokeAI/images, SkyReels, LightOnOCR, HeartMula, and PersonaPlex contention.

`ada2` now has 128 GB system RAM plus an RTX 6000 Ada 48 GB. Treat the extra host RAM as CPU-offload, model-load, compile-cache, and artifact-processing headroom. It does not change the primary scheduling constraint for CUDA backends: VRAM is still the limiting resource, especially when SkyReels, HeartMula, PersonaPlex, or InvokeAI move from idle to generation.

## What To Verify Before Marking Ready

For each backend:

1. Confirm the advertised base URL is reachable from the gateway host.
2. Confirm `/readyz` fails when required model artifacts or upstreams are missing.
3. Run one real request, not only `/health`.
4. Fetch any generated artifact through the service and, if applicable, through the gateway proxy.
5. Record idle and peak VRAM from `nvidia-smi`.
6. Check container logs for dependency downloads or warnings that will recur on every recreate.
7. Confirm the specialized UI calls lifecycle `ensure` before making a request.

## Findings From April 26, 2026 Bring-Up

### vLLM Strong On ada2

After the ada2 RAM upgrade, `vllm-strong` was restarted successfully from an exited 137 state. With `unsloth/Qwen3-30B-A3B-FP8`, `VLLM_MAX_MODEL_LEN=2048`, `VLLM_GPU_MEMORY_UTILIZATION=0.55`, and `VLLM_CPU_OFFLOAD_GB=8`, it reached `/v1/models` readiness and settled at about 27.1 GB VRAM used, leaving about 18.3 GB free on the 48 GB RTX 6000 Ada.

Operationally, 128 GB system RAM makes the current 8 GB vLLM CPU offload setting much safer and gives room to raise offload if future model/context choices require it. Do not treat that as free concurrency: CPU offload is slower than VRAM, and peak media jobs can still exceed the remaining VRAM while vLLM is resident.

### PersonaPlex

The Nexus PersonaPlex service is only a shim on `9160`. It does not start the upstream live UI/server on `8998`.

The upstream repo can build its own CUDA UI container from `.runtime/personaplex/app/docker-compose.yaml`, but it needs access to the gated `nvidia/personaplex-7b-v1` Hugging Face repo. On `ada2`, `HF_TOKEN` and `HUGGINGFACE_HUB_TOKEN` were empty and no cached artifacts existed, so the upstream server exited with a 401 while downloading `voices.tgz`.

Do not call PersonaPlex functional until:

- a token with accepted model access is installed,
- the upstream UI/server is managed by Nexus compose or lifecycle,
- `https://ada2:8998/` returns the UI,
- the lifecycle readiness check reflects the upstream runtime, not only the shim process.

### HeartMula

HeartMula required three checkpoint downloads into `.runtime/heartmula/ckpt`:

- `HeartMuLa/HeartMuLaGen`
- `HeartMuLa/HeartMuLa-oss-3B-happy-new-year`
- `HeartMuLa/HeartCodec-oss-20260123`

Initial failures were:

- missing `/data/ckpt/HeartMuLa-oss-3B`,
- wrapper passed `save_path` twice to upstream `postprocess()`,
- current `torchaudio.save()` path required `torchcodec`.

The wrapper now falls back to `soundfile` for the TorchCodec save failure. A 4 second generation smoke test completed and returned a 783 KB WAV. Observed VRAM on `ada2` was about 22 GB idle with eager load, about 1 GB idle after switching to lazy load, and about 35 GB during generation. Use `HEARTMULA_LAZY_LOAD=true` for lifecycle-managed use.

### LightOnOCR

LightOnOCR was already active and ready. A real OCR request against a generated text image returned `HELLO NEXUS OCR` through the native CUDA path. Its idle container does not hold persistent VRAM because the model loads inside the request subprocess.

### SkyReels

Earlier SkyReels UI failures came from the gateway normalizer treating UI payloads with `backend_class` as advanced payloads and routing to the larger default model. UI payloads now select the smaller DF 1.3B 540P path and persisted artifacts are served through stable output URLs.

## Common Failure Modes

- Empty secrets that look configured because the env keys exist.
- Gated model repos without accepted license access.
- Model artifacts downloaded into the wrong directory shape.
- Containers that clone repos and install large dependency stacks at startup.
- Shims that report process health while the real upstream model/UI is missing.
- Browser UI links derived from internal container hostnames or localhost.
- GPU services that pass direct health checks but fail real requests due OOM.
- Artifact URLs that point at container-local paths or vanish after request cleanup.
- Gateway disabled-backend settings that keep a newly working backend invisible.

## Convergent Approach

For a new backend, build the smallest reliable path first:

1. Choose host and tier before writing compose.
2. Put model artifact requirements in the README and lifecycle notes.
3. Prefer baking dependencies into the image over startup `pip install`.
4. Make `/readyz` assert the exact prerequisites for a real request.
5. Add lifecycle metadata with conservative memory estimates.
6. Add a specialized UI `data-lifecycle-ensure` hook if there is a UI.
7. Run one real smoke request and document the command and result.

If the backend is too complex for a simple heuristic, use the lifecycle manager's LLM advisor only as an advisory layer. The deterministic tier, memory, health, inflight-job, and recent-request rules should remain the source of truth for start/stop actions.
