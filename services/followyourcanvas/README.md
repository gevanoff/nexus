# FollowYourCanvas Service

Containerized video-generation shim exposing `POST /v1/videos/generations`.

The container is Nexus-owned. The upstream FollowYourCanvas repo is still cloned into the persistent data volume on startup, but invocation is now handled by a built-in runner inside the image rather than an operator-supplied command string.

## Runtime

- Recommended host: `ada2`
- Default port: `9165`

## Performance note

No clear container-specific performance regression is expected on Linux/NVIDIA. The main risk is upstream GPU/runtime compatibility, not container overhead.

The upstream project recommends at least 60 GB of GPU memory for inference. `ada2` has 48 GB, so successful startup does not guarantee a full generation will fit.

## Default config

Nexus creates `infer-configs/prompt-panda-nexus.yaml` inside the upstream checkout on first start and uses it as `FYC_DEFAULT_CONFIG`.

Expected model layout under the upstream checkout:

```text
pretrained_models/
  Qwen-VL-Chat/
  stable-diffusion-2-1/
  follow-your-canvas/checkpoint-40000.ckpt
  sam/sam_vit_b_01ec64.pth
```
