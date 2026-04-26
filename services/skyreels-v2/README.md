# SkyReels V2 Service

Containerized video-generation shim exposing `POST /v1/videos/generations`.

The container is Nexus-owned. The upstream SkyReels-V2 repo is still cloned into the persistent data volume on startup, but invocation is now handled by a built-in runner inside the image rather than an operator-supplied command string.

## Runtime

- Recommended host: `ada2`
- Default port: `9180`
- Hugging Face cache is expected to live under the persistent `/data/cache` volume so model downloads survive container recreation.

## Verification

Run the user-facing smoke test from a gateway host:

```bash
./deploy/scripts/smoke-test-video.sh
```

Relevant cache env overrides:

- `SKYREELS_HF_HOME`
- `SKYREELS_HUGGINGFACE_HUB_CACHE`
- `SKYREELS_TRANSFORMERS_CACHE`
- `SKYREELS_XDG_CACHE_HOME`
- `SKYREELS_TORCH_HOME`

## Performance note

No clear container-specific performance regression is expected on Linux/NVIDIA. The main risks remain upstream CUDA and driver compatibility.
