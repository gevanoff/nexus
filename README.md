# HF NVIDIA-Nemotron-Nano-9B-v2 Nexus Integration Workspace

This coding workspace was generated for integrating the HuggingFace model `nvidia/NVIDIA-Nemotron-Nano-9B-v2` into Nexus.

## Summary

- Source: https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2
- Route kind: `chat`
- Runtime strategy: `transformers`
- Runtime rationale: Model metadata points at a format or multimodal architecture that should not be treated as a plain vLLM text backend.
- Containerize: `true`
- Shim required: `true`
- Service name: `hf-nvidia-nvidia-nemotron-nano-9b-v2`
- Backend class: `hf_nvidia_nvidia_nemotron_nano_9b_v2`
- Target API path: `/v1/chat/completions`

## Recommended Deployment Target

- Host: `ai1`
- Lane: `vLLM Fast`
- Deployment mode: `compose`
- Comparable VRAM: `22000` MB
- Reason: Fallback text/json shims should start from ai1 unless the model clearly needs the heavier ada2 lane or an MLX host-native path.

## Metadata Notes

- library: `transformers`
- pipeline: `text-generation`
- gated: `false`
- private: `false`

Warnings:
- none
