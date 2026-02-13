# Nexus Services

This directory contains all containerized services in the Nexus infrastructure.

## Overview

Each service is:
- **Independent**: Can be built and run standalone
- **Discoverable**: Exposes `/v1/metadata` for capability advertisement
- **Observable**: Provides `/health` and `/readyz` endpoints
- **Standardized**: Follows OpenAI API conventions where applicable
- **Registrable**: Base URLs are published to etcd for multi-host discovery

## Available Services

### Core Services

#### Gateway (`gateway/`)
- **Purpose**: Central API gateway and request router
- **Ports**: 8800 (API), 8801 (observability)
- **Capabilities**: Chat, routing, auth, service discovery
- **Status**: ✅ Implemented
- **Documentation**: [gateway/README.md](gateway/README.md)

#### Ollama (`ollama/`)
- **Purpose**: Large language model inference
- **Port**: 11434
- **Capabilities**: Chat completions, text completions, embeddings
- **Status**: 🔧 Configuration only (uses official image)
- **Documentation**: [ollama/README.md](ollama/README.md)

### AI Services

#### Images (`images/`)
- **Purpose**: Text-to-image generation
- **Port**: 7860
- **Capabilities**: Image generation (SDXL, DALL-E style)
- **Status**: ✅ Implemented (shim; stub-by-default)
- **Documentation**: [images/README.md](images/README.md)

#### TTS (`tts/`)
- **Purpose**: Text-to-speech synthesis
- **Port**: 9940
- **Capabilities**: Audio generation from text
- **Status**: ✅ Implemented (Pocket TTS shim)
- **Documentation**: [tts/README.md](tts/README.md)

### Development

#### Template (`template/`)
- **Purpose**: Starting point for new services
- **Status**: 📚 Reference implementation
- **Documentation**: [template/README.md](template/README.md)

## Service Architecture

```
┌─────────────────────────────────────────┐
│            Gateway Service              │
│  • Authentication                       │
│  • Request routing                      │
│  • Service discovery                    │
│  • API aggregation                      │
└──────┬─────────┬──────────┬─────────────┘
       │         │          │
       ▼         ▼          ▼
┌──────────┐ ┌──────┐ ┌────────┐
│  Ollama  │ │Images│ │  TTS   │
│   LLM    │ │ Gen  │ │ Audio  │
└──────────┘ └──────┘ └────────┘
```

For multi-host rollouts, use the per-service manifests in `deploy/` to run individual services on separate hosts.

## Required Endpoints

All services MUST implement these endpoints:

1. **`GET /health`**
   - Liveness check
   - Returns 200 if process is running
   - Should not check dependencies

2. **`GET /readyz`**
   - Readiness check
   - Returns 200 if service can handle requests
   - Should check critical dependencies

3. **`GET /v1/metadata`**
   - Service discovery
   - Returns capabilities, endpoints, configuration options
   - Follows standardized schema (see SERVICE_API_SPECIFICATION.md)

See [../SERVICE_API_SPECIFICATION.md](../SERVICE_API_SPECIFICATION.md) for complete specification.

## Adding a New Service

### 1. Create Service Directory

```bash
mkdir services/my-service
cd services/my-service
```

### 2. Copy Template Files

```bash
cp ../template/example-service.py app/main.py
cp ../template/.env.example .env.example
```

### 3. Create Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Expose port
EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=10s \
    CMD curl -f http://localhost:9000/health || exit 1

# Run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
```

### 4. Add to Docker Compose

Create a new per-component compose file (policy: one file per service), e.g. `docker-compose.my-service.yml`:

```yaml
my-service:
  build:
    context: ./services/my-service
  container_name: nexus-my-service
  ports:
    - "9000:9000"
  environment:
    - SERVICE_NAME=my-service
  networks:
    - nexus
  restart: unless-stopped
```

### 5. Implement Required Endpoints

Edit `app/main.py` to implement:
- `/health` - Liveness check
- `/readyz` - Readiness check  
- `/v1/metadata` - Service metadata

### 6. Add Service-Specific Endpoints

Implement your service's functionality following OpenAI conventions:
- `/v1/chat/completions` for chat
- `/v1/images/generations` for images
- `/v1/audio/speech` for TTS
- etc.

### 7. Update Gateway

Add backend URL to gateway environment:

```yaml
gateway:
  environment:
    - MY_SERVICE_BASE_URL=http://my-service:9000
```

### 8. Test

```bash
# Start service
docker compose up -d my-service

# Test health
curl http://localhost:9000/health

# Test metadata
curl http://localhost:9000/v1/metadata
```

## Service Development Guidelines

### Configuration
- Use environment variables for all configuration
- Provide `.env.example` with all variables documented
- Set sensible defaults
- Validate configuration on startup

### Logging
- Use structured logging (JSON format)
- Include correlation IDs from headers
- Log all errors with stack traces
- Don't log sensitive data (tokens, passwords, etc.)

### Error Handling
- Use standard HTTP status codes
- Return error details in response body
- Handle timeouts gracefully
- Provide meaningful error messages

### Security
- Run as non-root user in containers
- Use minimal base images (slim, alpine, distroless)
- Don't expose unnecessary ports
- Validate all inputs
- Sanitize outputs

### Performance
- Implement request timeouts
- Use connection pooling
- Handle backpressure
- Clean up resources properly

### Testing
- Unit tests for business logic
- Integration tests with dependencies
- Health endpoint tests
- Load/stress tests

## Service Communication

Services communicate over the internal Docker network:

- **Service-to-Service**: HTTP over internal network
- **Client-to-Gateway**: HTTPS with authentication
- **Gateway-to-Services**: HTTP (internal network, no auth needed)

Service DNS names match their docker compose service names:
- `http://ollama:11434`
- `http://images:7860`
- `http://tts:9940`

## Resource Management

Specify resource requirements in metadata:

```json
{
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": "nvidia-gpu",
    "gpu_memory": "8Gi"
  }
}
```

Configure in docker compose:

```yaml
deploy:
  resources:
    limits:
      cpus: '2'
      memory: 4G
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

## Health Checks

### Liveness (`/health`)
- Process is running
- Quick check (< 100ms)
- Always returns 200 when alive
- Used for restart decisions

### Readiness (`/readyz`)
- Service can handle requests
- Checks dependencies
- May take longer (< 5s)
- Used for routing decisions

Example implementation:

```python
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/readyz")
async def readiness():
    checks = {}
    
    # Check backend
    try:
        # Test backend connection
        checks["backend"] = "ok"
    except:
        checks["backend"] = "error"
    
    # Check model loaded
    checks["model"] = "loaded" if model_loaded else "not_loaded"
    
    all_ok = all(v in ["ok", "loaded"] for v in checks.values())
    status_code = 200 if all_ok else 503
    
    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks
    }
```

## Monitoring

### Metrics
Expose Prometheus metrics at `/metrics`:

```python
from prometheus_client import Counter, Histogram, generate_latest

requests_total = Counter('requests_total', 'Total requests')
request_duration = Histogram('request_duration_seconds', 'Request duration')

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")
```

### Logging
Use structured logging:

```python
import logging
import json

logger = logging.getLogger(__name__)

logger.info(json.dumps({
    "event": "request_processed",
    "request_id": request_id,
    "duration_ms": duration,
    "status": status_code
}))
```

## Service Discovery

The gateway discovers services via `/v1/metadata`:

1. Gateway queries each service's metadata endpoint
2. Services respond with capabilities, endpoints, options
3. Gateway builds routing table
4. Gateway validates requests against capabilities
5. Gateway routes to appropriate service

Example metadata:

```json
{
  "schema_version": "v1",
  "service": {
    "name": "my-service",
    "version": "1.0.0"
  },
  "capabilities": {
    "domains": ["image"],
    "modalities": ["image"],
    "streaming": false
  },
  "endpoints": [
    {
      "path": "/v1/images/generations",
      "method": "POST",
      "operation_id": "images.generate"
    }
  ]
}
```

## Deployment

### Development
```bash
docker compose up -d
```

### Production
```bash
# Build images
docker compose build

# Start services
docker compose up -d

# Scale specific service
docker compose up -d --scale ollama=3
```

### Multi-host / Alternative Runtimes
See `deploy/` for per-service Docker Compose and containerd (nerdctl) manifests.

## Troubleshooting

### Service won't start
```bash
# Check logs
docker compose logs my-service

# Check configuration
docker compose config

# Check health
curl http://localhost:9000/health
```

### Can't connect to service
```bash
# Verify service is running
docker compose ps

# Test from gateway
docker compose exec gateway curl http://my-service:9000/health

# Check network
docker network inspect nexus_nexus
```

### Performance issues
```bash
# Check resource usage
docker stats

# Check logs for errors
docker compose logs --tail=100 my-service

# Check metrics
curl http://localhost:9000/metrics
```

## Examples

See these services for reference:

- **Minimal service**: `template/example-service.py`
- **Full gateway**: `gateway/app/main.py`
- **Service wrapper**: `ollama/` (wraps existing service)

## Resources

- [SERVICE_API_SPECIFICATION.md](../SERVICE_API_SPECIFICATION.md)
- [ARCHITECTURE.md](../ARCHITECTURE.md)
- [Docker Compose docs](https://docs.docker.com/compose/)
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
