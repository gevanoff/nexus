# Ollama Service

Ollama is a large language model inference service that provides OpenAI-compatible APIs.

## Placement Policy

- Ollama should run host-native on macOS bare metal when Apple Silicon acceleration is desired.
- CPU-only backends that do not benefit from NVIDIA acceleration should run as containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated workloads should run on Linux/NVIDIA hosts.

## Overview

Nexus supports two operational modes:

1. **Containerized Ollama** (Linux container, default compose component)
2. **Native Ollama on macOS Apple Silicon** (recommended when you want Apple-accelerated inference)

For Apple Silicon acceleration, run Ollama natively on macOS and point Gateway at that host via `OLLAMA_BASE_URL`.

## Features

- **Multiple Models**: Supports Llama, Qwen, Mistral, Gemma, and more
- **Streaming**: Real-time streaming responses
- **OpenAI Compatible**: Drop-in replacement for OpenAI API
- **GPU Accelerated**: Native Apple Silicon acceleration (macOS host-native) or NVIDIA acceleration (Linux + NVIDIA container runtime)
- **Model Management**: Pull and manage models via API

## Configuration

Containerized Ollama is configured in `docker-compose.ollama.yml` with:

```yaml
ollama:
  image: ollama/ollama:latest
  ports:
    - "11434:11434"
  environment:
    - OLLAMA_HOST=0.0.0.0:11434
  volumes:
    - ./.runtime/ollama:/root/.ollama
```

Gateway target is controlled with `OLLAMA_BASE_URL` (set in `.env`):

```bash
# Containerized Ollama (same compose project)
OLLAMA_BASE_URL=http://ollama:11434

# Native macOS Ollama on same machine as Docker Desktop
OLLAMA_BASE_URL=http://host.docker.internal:11434

# Native macOS Ollama on remote accelerator host
OLLAMA_BASE_URL=http://<mac-host-or-ip>:11434
```

Install host-native Ollama on macOS with:

```bash
./services/ollama/scripts/install-native-macos.sh --host 127.0.0.1 --port 11434
```

## Usage

### Pull Models

```bash
# Via docker exec
docker compose exec ollama ollama pull llama3.1:8b

# List downloaded models
docker compose exec ollama ollama list

# Remove a model
docker compose exec ollama ollama rm llama3.1:8b
```

### API Usage

The Ollama API is accessible at `http://localhost:11434` or internally at `http://ollama:11434`.

#### List Models

```bash
curl http://localhost:11434/api/tags
```

#### Generate Completion

```bash
curl -X POST http://localhost:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "prompt": "Why is the sky blue?"
  }'
```

#### Chat Completion

```bash
curl -X POST http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

#### Streaming Response

```bash
curl -X POST http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "user", "content": "Tell me a story"}
    ],
    "stream": true
  }'
```

### Via Gateway

Access Ollama through the Nexus gateway at `http://localhost:8800`:

```bash
# Chat completion via gateway (requires auth token)
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

## Recommended Models

### General Purpose
- `llama3.1:8b` - Fast, good quality (8B parameters)
- `llama3.1:70b` - High quality (70B parameters, requires 40GB+ VRAM)
- `qwen2.5:14b` - Balanced performance (14B parameters)

### Specialized
- `llama3.1:8b-instruct` - Instruction tuned
- `codellama:13b` - Code generation
- `mistral:7b` - Fast inference
- `gemma:7b` - Google's model

### Small/Fast
- `llama3.1:3b` - Very fast (3B parameters)
- `phi3:3.8b` - Microsoft's efficient model
- `tinyllama:1.1b` - Tiny but capable

Pull models with:
```bash
docker compose exec ollama ollama pull <model-name>
```

## GPU Support

### Apple Silicon (macOS host-native)

- Ollama can use Apple Silicon acceleration when running natively on macOS.
- Linux containers do not provide direct Metal/ANE passthrough for Ollama workloads.
- Recommended pattern: run Ollama on the macOS host and access it from Gateway via HTTP.

### Requirements

- NVIDIA GPU
- NVIDIA Docker runtime installed
- CUDA drivers

### Verify GPU Access

```bash
# Check GPU is visible in container
docker compose exec ollama nvidia-smi

# Check Ollama is using GPU
docker compose logs ollama | grep -i gpu
```

### Disable GPU (CPU Only)

To run on CPU only, remove the deploy section from docker-compose.ollama.yml:

```yaml
ollama:
  image: ollama/ollama:latest
  # Remove or comment out deploy section
  # deploy:
  #   resources:
  #     reservations:
  #       devices:
  #         - driver: nvidia
```

## Storage

Models are stored on the host under `nexus/.runtime/ollama` (bind-mounted into the container at `/root/.ollama`).

To back up models:
```bash
# Create backup
docker run --rm -v "$(pwd)/.runtime/ollama:/data:ro" -v "$(pwd):/backup" \
  alpine tar czf /backup/ollama-models-backup.tar.gz -C /data .

# Restore from backup
docker run --rm -v "$(pwd)/.runtime/ollama:/data" -v "$(pwd):/backup" \
  alpine tar xzf /backup/ollama-models-backup.tar.gz -C /data
```

## Performance Tuning

### Context Length

Set via environment variable:
```yaml
environment:
  - OLLAMA_NUM_CTX=4096  # Context window size
```

### Concurrent Requests

```yaml
environment:
  - OLLAMA_MAX_LOADED_MODELS=2  # Number of models to keep in memory
  - OLLAMA_NUM_PARALLEL=4       # Parallel request processing
```

### Memory Management

```yaml
environment:
  - OLLAMA_KEEP_ALIVE=5m  # Keep models in memory for 5 minutes
```

## API Endpoints

Ollama provides these endpoints:

- `POST /api/generate` - Generate completion
- `POST /api/chat` - Chat completion
- `POST /api/embeddings` - Generate embeddings
- `GET /api/tags` - List models
- `POST /api/pull` - Download a model
- `DELETE /api/delete` - Remove a model
- `POST /api/push` - Push a model (requires Ollama account)
- `POST /api/copy` - Copy a model
- `POST /api/show` - Show model info

Full API docs: https://github.com/ollama/ollama/blob/main/docs/api.md

## Health Checks

### Check Service Health

```bash
curl http://localhost:11434/api/tags
```

Should return list of models if healthy.

### Via Docker

```bash
docker compose ps ollama
```

Should show "healthy" status.

## Troubleshooting

### Migration from containerized to host-native

```bash
# 1) Install/start native Ollama on macOS
./services/ollama/scripts/install-native-macos.sh --host 127.0.0.1 --port 11434

# 2) Verify local health on macOS host
curl -fsS http://127.0.0.1:11434/api/version

# 3) Update nexus/.env
# OLLAMA_BASE_URL=http://host.docker.internal:11434

# 4) Start Nexus without ollama container
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --build

# 5) Verify gateway contract using external/native Ollama
./deploy/scripts/verify-gateway.sh --external-ollama
```

### Native-macOS security baseline

- Run Ollama under a dedicated non-admin user.
- Restrict listener exposure (loopback or LAN allowlist only).
- Apply host firewall rules to allow only Gateway/control-plane source IPs.
- Keep model directories owned by the service account with least-privilege permissions.

### Service won't start

```bash
# Check logs
docker compose logs ollama

# Check if port is in use
lsof -i :11434

# Restart service
docker compose restart ollama
```

### GPU not detected

```bash
# Check NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# Check GPU is available
nvidia-smi

# Update docker compose to use GPU
```

### Out of memory

```bash
# Check GPU memory
nvidia-smi

# Use smaller model
docker compose exec ollama ollama pull llama3.1:3b

# Reduce context window
# Set OLLAMA_NUM_CTX to lower value (e.g., 2048)
```

### Model download fails

```bash
# Check network
curl -I https://ollama.com

# Check disk space
docker system df

# Retry pull
docker compose exec ollama ollama pull llama3.1:8b
```

## Integration with Gateway

The gateway automatically discovers Ollama via its health endpoint and routes chat/completion requests to it.

Gateway configuration:
```yaml
environment:
  - OLLAMA_BASE_URL=http://ollama:11434
  - DEFAULT_BACKEND=ollama
```

## Model Management Script

Create `manage-models.sh`:

```bash
#!/bin/bash
# Manage Ollama models

case "$1" in
  pull)
    docker compose exec ollama ollama pull "$2"
    ;;
  list)
    docker compose exec ollama ollama list
    ;;
  remove)
    docker compose exec ollama ollama rm "$2"
    ;;
  info)
    docker compose exec ollama ollama show "$2"
    ;;
  *)
    echo "Usage: $0 {pull|list|remove|info} [model-name]"
    exit 1
    ;;
esac
```

Usage:
```bash
chmod +x manage-models.sh
./manage-models.sh pull llama3.1:8b
./manage-models.sh list
```

## Resources

- [Ollama Documentation](https://github.com/ollama/ollama)
- [Ollama Model Library](https://ollama.com/library)
- [Ollama API Reference](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Docker Hub](https://hub.docker.com/r/ollama/ollama)
