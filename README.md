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
