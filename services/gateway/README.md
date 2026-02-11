# Gateway Service

The Nexus Gateway is the central API gateway that provides:
- OpenAI-compatible endpoints for AI services
- Authentication and authorization
- Request routing to backend services
- Service discovery via `/v1/metadata` and `/v1/descriptor`
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
- `GET /v1/descriptor` - Enhanced descriptor (response types + UI placement hints)

### OpenAI-Compatible Endpoints

- `GET /v1/models` - List available models
- `POST /v1/chat/completions` - Create chat completion (streaming supported)

### Dynamic Backend Catalog/UI Endpoints

- `GET /v1/registry` - List service records from the gateway registry
- `GET /v1/backends/catalog` - Return backend descriptors and endpoint/capability contracts
- `GET /v1/ui/layout` - Return gateway-generated UI layout (Chat primary + specialized backend panels)

## Configuration

In Nexus, a single env file (`nexus/.env`) is mounted into the container at `/var/lib/gateway/app/.env`.
Most gateway settings are configured via environment variables.

Environment variables (common subset):

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

# Service discovery (etcd)
ETCD_ENABLED=true
ETCD_URL=http://etcd:2379
ETCD_PREFIX=/nexus/services/
ETCD_POLL_INTERVAL=15
ETCD_SEED_FROM_ENV=true

# Features
MEMORY_V2_ENABLED=true
METRICS_ENABLED=true

# Data persistence
MEMORY_DB_PATH=/var/lib/gateway/data/memory.sqlite
USER_DB_PATH=/var/lib/gateway/data/users.sqlite

# Operator config (mounted read-only from the host)
MODEL_ALIASES_PATH=/var/lib/gateway/config/model_aliases.json
AGENT_SPECS_PATH=/var/lib/gateway/config/agent_specs.json
TOOLS_REGISTRY_PATH=/var/lib/gateway/config/tools_registry.json
```

### Persistence Layout (Host ↔ Container)

Nexus keeps state and large artifacts on the host under `nexus/.runtime/`.

- RW data: `nexus/.runtime/gateway/data/` → `/var/lib/gateway/data`
- RO operator config: `nexus/.runtime/gateway/config/` → `/var/lib/gateway/config`

Config files are seeded (once) by the setup scripts:
- `tools_registry.json`
- `model_aliases.json`
- `agent_specs.json`

## Usage

### Docker

```bash
# The Nexus gateway image is built from the repo root so it can package the full
# gateway implementation under ./gateway/.

# Build (from repo root)
docker build -f nexus/services/gateway/Dockerfile -t nexus-gateway .

# Run
docker run -p 8800:8800 -p 8801:8801 \
  -e GATEWAY_BEARER_TOKEN=secret \
  -e OLLAMA_BASE_URL=http://ollama:11434 \
  nexus-gateway
```

### Docker Compose

The gateway is included in the main `docker-compose.yml`. Start with:

```bash
docker compose up gateway
```

### Local Development

The gateway source is vendored into this repo under `services/gateway/app` and `services/gateway/tools`.

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

### Backend Catalog

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8800/v1/backends/catalog
```

### Dynamic UI Layout

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8800/v1/ui/layout
```

## Implementation

Nexus builds and runs the full gateway implementation from the repo’s top-level `gateway/` directory.
The container runtime layout is kept compatible with the gateway’s expected paths under `/var/lib/gateway/*`.

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
docker compose logs gateway

# Verify configuration
docker compose exec gateway env | grep GATEWAY
```

### Can't connect to Ollama

```bash
# Check if Ollama is running
docker compose ps ollama

# Test connectivity from gateway
docker compose exec gateway curl http://ollama:11434/api/tags
```

### Authentication errors

```bash
# Verify token is set
docker compose exec gateway env | grep GATEWAY_BEARER_TOKEN

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
