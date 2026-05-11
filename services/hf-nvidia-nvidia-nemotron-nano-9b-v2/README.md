# hf-nvidia-nvidia-nemotron-nano-9b-v2

NVIDIA Nemotron Nano 9B v2 chat backend adapter for Nexus.

## Overview

- Model: `nvidia/NVIDIA-Nemotron-Nano-9B-v2`
- Runtime: `transformers` (AutoModelForCausalLM + AutoTokenizer)
- API: OpenAI-compatible `/v1/chat/completions`
- Containerized: Yes (CUDA 12.4 base)

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/health` | GET | Basic health check |
| `/readyz` | GET | Readiness (503 until model loaded) |
| `/v1/metadata` | GET | Model metadata |
| `/v1/models` | GET | List models |
| `/v1/chat/completions` | POST | Chat completion |

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `HF_MODEL_ID` | `nvidia/NVIDIA-Nemotron-Nano-9B-v2` | Model ID |
| `DEVICE` | `cuda` | Inference device |
| `MAX_NEW_TOKENS` | `2048` | Max output tokens |
| `TEMPERATURE` | `0.7` | Sampling temp |
| `TOP_P` | `0.9` | Nucleus sampling |
| `TOP_K` | `50` | Top-k sampling |
| `HUGGINGFACE_HUB_TOKEN` | | HF token (if gated) |

## Deployment

```bash
docker compose -f docker-compose.hf-nvidia-nvidia-nemotron-nano-9b-v2.yml up -d
```

Requires NVIDIA Container Toolkit.