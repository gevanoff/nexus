# HF GLM-5.3-mixed-4_5bit Nexus Integration Workspace

This coding workspace was generated for integrating the HuggingFace model `mlx-community/GLM-5.3-mixed-4_5bit` into Nexus.

## Summary

- Source: https://huggingface.co/mlx-community/GLM-5.3-mixed-4_5bit
- Route kind: `chat`
- Runtime strategy: `mlx`
- Runtime rationale: Classified as `chat` from causal/text-generation metadata. MLX metadata selects the host-native Apple Silicon runtime.
- Integration strategy: `host_native_runtime` - New backend or host-native runtime integration.
- Containerize: `false`
- Shim required: `false`
- Service name: `hf-mlx-community-glm-5-3-mixed-4-5bit`
- Backend class: `hf_mlx_community_glm_5_3_mixed_4_5bit`
- Target API path: `/v1/chat/completions`

## Recommended Deployment Target

- Host: `ai2`
- Lane: `MLX`
- Deployment mode: `host_native`
- Comparable VRAM: `0` MB
- Reason: MLX models should stay on ai2 because that host is the Apple Silicon M3 Ultra lane with 512GB unified memory and a host-native MLX serving path.

## Metadata Notes

- library: `mlx`
- pipeline: `text-generation`
- gated: `false`
- private: `false`

Warnings:
- none
