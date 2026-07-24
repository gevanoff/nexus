# Nexus Media Generation Services

This directory provides a common Nexus-owned API layer for three upstream CUDA media runtimes:

| Component | Host | GPU | Port | Backend class |
|---|---|---:|---:|---|
| LTX-2.3 22B Distilled | `ada2` | RTX 6000 Ada, physical GPU 0 | 9180 | `ltx_video` |
| HunyuanVideo-1.5 | `stackrot` | RTX 3090, physical GPU 1 | 9185 | `hunyuan_video` |
| ACE-Step 1.5 XL SFT | `stackrot` | RTX 3090, physical GPU 1 | 9195 | `ace_step_music` |

The services expose consistent Nexus endpoints while preserving the official upstream runtimes:

- `GET /healthz`
- `GET /readyz`
- `GET /v1/models`
- `POST /v1/videos/generations` for LTX and HunyuanVideo
- `POST /v1/music/generations` for ACE-Step
- `GET /outputs/{job_id}/{filename}`

## Resource policy

These services are optional lifecycle-managed backends rather than permanent residents.

- LTX requires an exclusive or near-exclusive window on `ada2`. Stop or unload large InvokeAI and vLLM residents before starting it when free VRAM is below the lifecycle target.
- HunyuanVideo and ACE-Step are both pinned to `stackrot` physical GPU 1. They must not run together.
- Qwen3-TTS also uses stackrot GPU 1 and normally must be stopped before either HunyuanVideo or ACE-Step starts.
- `stackrot` physical GPU 0 remains assigned to `nexus-vllm-fast`.

The lifecycle manifest marks all three services `auto_start: false`, `auto_stop: true`, and `requires_confirmation: true`.

## Persistent storage

The upstream checkouts, models, caches, and generated outputs live outside the repository:

```text
ada2:
  /data/ltx-video/
    app/
    models/
    outputs/

stackrot:
  /data/hunyuan-video/
    app/
    models/HunyuanVideo-1.5/
    outputs/

  /data/ace-step/
    app/
    models/
    outputs/
```

All services reuse `/data/huggingface` as the shared Hugging Face cache.

## Model provisioning

The containers clone and install the upstream applications, but large model artifacts are deliberately not downloaded during image builds. This keeps Docker layers reproducible and prevents repeated multi-gigabyte downloads.

### LTX-2.3

Provision these paths under `/data/ltx-video/models`, or override the corresponding environment variables:

```text
LTX_DISTILLED_CHECKPOINT_PATH=/data/models/ltx-2.3-22b-distilled-1.1.safetensors
LTX_SPATIAL_UPSAMPLER_PATH=/data/models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors
LTX_GEMMA_ROOT=/data/models/gemma-3-12b-it-qat-q4_0-unquantized
```

The mounted container path `/data/models` corresponds to host path `/data/ltx-video/models`.

### HunyuanVideo-1.5

Provision the official model repository at:

```text
/data/hunyuan-video/models/HunyuanVideo-1.5
```

The container sees this as:

```text
HUNYUAN_MODEL_PATH=/data/models/HunyuanVideo-1.5
```

### ACE-Step 1.5

ACE-Step uses the upstream model identifiers by default and downloads them into the persistent Hugging Face cache:

```text
ACE_STEP_DIT_MODEL=acestep-v15-xl-sft
ACE_STEP_LM_MODEL=acestep-5Hz-lm-1.7B
ACESTEP_LM_BACKEND=vllm
```

## Deployment

Deploy the optional services explicitly rather than deploying the full host topology with all GPU contenders running:

```bash
# ada2
./deploy/scripts/deploy.sh \
  --topology-host ada2 \
  --component ltx-video \
  prod main

# stackrot: choose one GPU-1 workload at a time
./deploy/scripts/deploy.sh \
  --topology-host stackrot \
  --component hunyuan-video \
  prod main

./deploy/scripts/deploy.sh \
  --topology-host stackrot \
  --component ace-step \
  prod main
```

After changing the remote-backend set, reinstall/reload ai2's host-side port proxy:

```bash
sudo ./deploy/scripts/install-backend-port-proxy-launchd.sh --user ai
```

## Verification

```bash
VIDEO_SMOKE_BACKEND_CLASS=ltx_video \
  ./deploy/scripts/smoke-test-video.sh

VIDEO_SMOKE_BACKEND_CLASS=hunyuan_video \
  ./deploy/scripts/smoke-test-video.sh

./deploy/scripts/smoke-test-music.sh
```

A service reports `503` from `/readyz` when the upstream checkout or required model paths are absent. That is intentional: registration must not advertise a model that cannot generate.
