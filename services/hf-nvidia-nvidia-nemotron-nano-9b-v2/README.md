# hf-nvidia-nvidia-nemotron-nano-9b-v2

NVIDIA Nemotron Nano 9B v2 chat backend adapter for Nexus.

## Quick Start

```bash
docker compose -f docker-compose.hf-nvidia-nvidia-nemotron-nano-9b-v2.yml up -d
```

## Endpoints

- `GET /health` - Health check
- `GET /readyz` - Readiness (503 until model loaded)
- `GET /v1/metadata` - Model info
- `GET /v1/models` - List models
- `POST /v1/chat/completions` - Chat completions (OpenAI-compatible)

## Environment

See `.env.example` for configuration. Key variables:
- `HF_MODEL_ID` - Model to load
- `DEVICE` - Inference device (default: cuda)
- `MAX_NEW_TOKENS` - Max output tokens
- `HUGGINGFACE_HUB_TOKEN` - HF token (if needed)

## Requirements

- NVIDIA GPU with ~22GB+ VRAM
- NVIDIA Container Toolkit
- CUDA 12.4+