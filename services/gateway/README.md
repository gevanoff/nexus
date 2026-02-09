# Gateway Service

The Nexus Gateway is the central API gateway that provides:
- OpenAI-compatible endpoints for AI services
- Authentication and authorization
- Request routing to backend services
- Service discovery via `/v1/metadata`
- Health monitoring and metrics

## Features

- **Unified API**: Single entry point for all AI services
- **Authentication**: Bearer token authentication
- **Service Discovery**: Auto-discovers backend capabilities
- **Health Checks**: Monitors backend service health
- **Metrics**: Prometheus-compatible metrics endpoint
- **Streaming**: Supports streaming responses for chat completions

## Endpoints

### Core Endpoints

- `GET /health` - Liveness check
- `GET /readyz` - Readiness check with backend validation
- `GET /v1/metadata` - Service capabilities and endpoint discovery

### OpenAI-Compatible Endpoints

- `GET /v1/models` - List available models
- `POST /v1/chat/completions` - Create chat completion (streaming supported)

## Configuration

Environment variables:

```bash
# Authentication
GATEWAY_BEARER_TOKEN=your-secret-token

# Server
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8800

# Observability
OBSERVABILITY_HOST=0.0.0.0
OBSERVABILITY_PORT=8801

# Backend services
OLLAMA_BASE_URL=http://ollama:11434
DEFAULT_BACKEND=ollama

# Features
MEMORY_V2_ENABLED=true
METRICS_ENABLED=true

# Data persistence
MEMORY_DB_PATH=/data/memory.sqlite
USER_DB_PATH=/data/users.sqlite
```

## Usage

### Docker

```bash
# Build
docker build -t nexus-gateway .

# Run
docker run -p 8800:8800 -p 8801:8801 \
  -e GATEWAY_BEARER_TOKEN=secret \
  -e OLLAMA_BASE_URL=http://ollama:11434 \
  nexus-gateway
```

### Docker Compose

The gateway is included in the main `docker-compose.yml`. Start with:

```bash
docker-compose up gateway
```

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment
export GATEWAY_BEARER_TOKEN=test-token
export OLLAMA_BASE_URL=http://localhost:11434

# Run
uvicorn app.main:app --host 0.0.0.0 --port 8800 --reload
```

## API Examples

### List Models

```bash
curl http://localhost:8800/v1/models \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Chat Completion

```bash
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

### Streaming Chat

```bash
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "user", "content": "Tell me a story"}
    ],
    "stream": true
  }'
```

### Service Discovery

```bash
curl http://localhost:8800/v1/metadata
```

## Extension Points

This is a minimal gateway implementation. For the full-featured gateway with:
- Tool bus and agent runtime
- Memory system (v1 and v2)
- Image generation support
- Audio/TTS support
- User authentication
- UI endpoints
- Policy-based routing
- Advanced metrics

See the original [gevanoff/gateway](https://github.com/gevanoff/gateway) repository.

## Architecture

The gateway acts as a reverse proxy and orchestrator:

```
Client Request
    ↓
Bearer Token Auth
    ↓
Request Routing
    ↓
Backend Service (Ollama, Images, etc.)
    ↓
Response (streaming or batch)
    ↓
Client Response
```

## Health Checks

The gateway provides two health endpoints:

1. **Liveness** (`/health`): Returns 200 if the process is running
2. **Readiness** (`/readyz`): Returns 200 only if backends are reachable

Use readiness for routing decisions and liveness for restart policies.

## Monitoring

### Metrics

Prometheus metrics available at `http://localhost:8801/metrics`

### Logs

Structured logging to stdout with:
- Request IDs
- Response times
- Status codes
- Backend information

## Security

- **Authentication**: All API endpoints require bearer token (except health/metadata)
- **Container Isolation**: Runs as non-root user in container
- **Network Isolation**: Only exposed via gateway, backends not directly accessible
- **Rate Limiting**: Can be added via configuration
- **IP Allowlisting**: Can be configured for sensitive endpoints

## Troubleshooting

### Gateway won't start

```bash
# Check logs
docker-compose logs gateway

# Verify configuration
docker-compose exec gateway env | grep GATEWAY
```

### Can't connect to Ollama

```bash
# Check if Ollama is running
docker-compose ps ollama

# Test connectivity from gateway
docker-compose exec gateway curl http://ollama:11434/api/tags
```

### Authentication errors

```bash
# Verify token is set
docker-compose exec gateway env | grep GATEWAY_BEARER_TOKEN

# Test with correct token
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8800/v1/models
```

## Development

### Adding New Endpoints

1. Add route handler in `app/main.py`
2. Add to metadata endpoint's `endpoints` list
3. Update `capabilities` if needed
4. Add tests

### Integrating New Backend

1. Add environment variable for backend URL
2. Add health check in `/readyz`
3. Add routing logic
4. Update metadata to advertise new capabilities

## Testing

```bash
# Run tests
pytest

# Test with real backend
pytest --backend-url http://ollama:11434
```

## License

See main repository LICENSE file.
