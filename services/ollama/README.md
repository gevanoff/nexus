# Ollama Service

Ollama is a large language model inference service that provides OpenAI-compatible APIs.

## Overview

This service uses the official Ollama Docker image with configuration for Nexus integration.

## Features

- **Multiple Models**: Supports Llama, Qwen, Mistral, Gemma, and more
- **Streaming**: Real-time streaming responses
- **OpenAI Compatible**: Drop-in replacement for OpenAI API
- **GPU Accelerated**: NVIDIA GPU support via Docker runtime
- **Model Management**: Pull and manage models via API

## Configuration

The Ollama service is configured in the main `docker-compose.yml` with:

```yaml
ollama:
  image: ollama/ollama:latest
  ports:
    - "11434:11434"
  environment:
    - OLLAMA_HOST=0.0.0.0:11434
  volumes:
    - ollama_data:/root/.ollama
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
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

To run on CPU only, remove the deploy section from docker-compose.yml:

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

Models are stored in a Docker volume (`ollama_data`) which persists across container restarts.

To back up models:
```bash
# Create backup
docker run --rm -v nexus_ollama_data:/data -v $(pwd):/backup \
  ubuntu tar czf /backup/ollama-models-backup.tar.gz -C /data .

# Restore from backup
docker run --rm -v nexus_ollama_data:/data -v $(pwd):/backup \
  ubuntu tar xzf /backup/ollama-models-backup.tar.gz -C /data
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
