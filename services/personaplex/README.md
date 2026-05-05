# PersonaPlex Service

Containerized PersonaPlex shim exposing `POST /v1/chat/completions`.

This port keeps the upstream proxy mode and optional startup bootstrap of the upstream `NVIDIA/personaplex` repo into the service data volume. It no longer accepts an operator-supplied subprocess command.

## Runtime

- Recommended host: `ada2`
- Default port: `9160`

## Host placement decision

PersonaPlex should run on `ada2` when it is enabled.

The upstream runtime is a real-time, full-duplex speech-to-speech model using
PyTorch on NVIDIA GPU acceleration. Its upstream Dockerfile is based on
`nvcr.io/nvidia/cuda:12.4.1-runtime-ubuntu22.04`, and the model card lists
PyTorch inference tested on NVIDIA A100 80GB. The published model artifact is a
7B model with a 16.7GB `model.safetensors` file plus tokenizer and runtime
overhead, so plan for substantially more than 16.7GB of available VRAM for
interactive service.

Current placement guidance:

- `ada2` is the appropriate production host because it has the only single GPU
  in the cluster with enough headroom class for this workload: RTX 6000 Ada
  48GB.
- `ai1` is not a good default while `vllm-fast`, embeddings, and SDXL-Turbo are
  resident. Its 24GB RTX 3090 is marginal for PersonaPlex and would likely need
  CPU offload after freeing other services.
- `ai2` has ample unified memory, but the tracked Nexus service is the upstream
  PyTorch/CUDA path, not an MLX port, so it is not the right host for this
  deployment.

Do not run PersonaPlex as another always-on GPU service on `ada2` while
`vllm-strong` reserves most of the same GPU. Either reserve a PersonaPlex window
by stopping/reducing `vllm-strong`, or introduce an explicit GPU scheduler before
enabling it continuously.

## Notes

- The shim container is Nexus-owned.
- The upstream PersonaPlex runtime is still fetched from its source repo when `PERSONAPLEX_REPO_URL` is set.
- REST proxying requires `PERSONAPLEX_UPSTREAM_BASE_URL`; otherwise use the live PersonaPlex UI directly.
- No intrinsic container performance issue is expected on Linux/NVIDIA; the main risk remains upstream runtime compatibility.

## Production Bring-Up Finding

Trading in the Nexus PersonaPlex component starts the shim on `9160`, but that shim does not start the upstream live Web UI/runtime on `8998`.

The upstream repo has its own CUDA Dockerfile and `docker-compose.yaml`. On `ada2`, after the shim has cloned the upstream repo, the live UI can be started from:

```bash
cd .runtime/personaplex/app
docker compose -p personaplex-upstream -f docker-compose.yaml up -d --build
```

The upstream model repo `nvidia/personaplex-7b-v1` is gated. In the April 26, 2026 smoke test, both `HF_TOKEN` and `HUGGINGFACE_HUB_TOKEN` were present but empty on `ada2`, no model artifacts were cached, and the upstream server exited with a Hugging Face 401 while downloading `voices.tgz`. A token with accepted access to `nvidia/personaplex-7b-v1` is required before this backend can become functional.
