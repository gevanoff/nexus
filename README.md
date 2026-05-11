# HF NVIDIA-Nemotron-Nano-9B-v2 Nexus Integration Workspace

Integrates `nvidia/NVIDIA-Nemotron-Nano-9B-v2` into Nexus as a chat backend.

## Summary

- Source: https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2
- Route kind: `chat`
- Runtime strategy: `transformers`
- Containerize: `true`
- Service name: `hf-nvidia-nvidia-nemotron-nano-9b-v2`
- Backend class: `hf_nvidia_nvidia_nemotron_nano_9b_v2`
- Target API path: `/v1/chat/completions`


## Recommended Deployment Target

- Host: `ai1`
- Lane: `vLLM Fast`
- Deployment mode: `compose`
- Comparable VRAM: `22000` MB


## API Surface
OpenAI-compatible chat completions at `/v1/chat/completions`.


### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HF_MODEL_ID` | `nvidia/NVIDIA-Nemotron-Nano-9B-v2` | Model to load |
| `DEVICE` | `cuda` | Device for inference |
| `MAX_NEW_TOKENS` | `2048` | Max generated tokens |
| `TEMPERATURE` | `0.7` | Sampling temperature |
| `TOP_P` | `0.9` | Nucleus sampling |
| `TOP_K` | `50` | Top-k sampling |
| `HUGGINGFACE_HUB_TOKEN` | | Required for gated models |
