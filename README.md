# Nexus AI Backend Integration

## NVIDIA Nemotron-Nano-9B-v2 Deployment

To deploy the NVIDIA Nemotron-Nano-9B-v2 model via vLLM Fast:
1. Ensure `.env` contains `VLLM_MODEL_FAST=nvidia/NVIDIA-Nemotron-Nano-9B-v2`
2. Run `docker-compose -f docker-compose.vllm-fast.yml up -d`

API Access:
- OpenAI-compatible endpoint: http://localhost:8001/v1/chat/completions
- Model name: `nvidia/NVIDIA-Nemotron-Nano-9B-v2`

Deployment details are configured in `docker-compose.vllm-fast.yml`. The model is integrated as a vLLM backend for OpenAI-style chat access.