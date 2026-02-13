# Service Template

This template provides a starting point for creating new Nexus services.

## Required Files

- `Dockerfile` - Container definition
- `app/main.py` - Service implementation with required endpoints
- `requirements.txt` - Python dependencies (if Python-based)
- `.env.example` - Configuration template
- `README.md` - Service documentation

## Required Endpoints

Every service must implement:

1. `GET /health` - Liveness check (200 = alive)
2. `GET /readyz` - Readiness check (200 = ready, 503 = not ready)
3. `GET /v1/metadata` - Service discovery (capabilities, endpoints, etc.)

See [SERVICE_API_SPECIFICATION.md](../../SERVICE_API_SPECIFICATION.md) for details.

## Template Structure

```
services/my-service/
├── Dockerfile              # Container build definition
├── docker-compose.<service>.yml  # Optional: standalone testing
├── requirements.txt        # Python dependencies
├── .env.example           # Configuration template
├── README.md              # Service documentation
└── app/
    ├── __init__.py        # Python package marker
    └── main.py            # Service implementation
```

## Example Service

See `example-service.py` for a minimal Python FastAPI service that implements all required endpoints.

## Creating a New Service

### 1. Copy Template

```bash
cp -r services/template services/my-service
cd services/my-service
```

### 2. Update Configuration

Edit `.env.example` with service-specific settings:

```bash
SERVICE_NAME=my-service
SERVICE_VERSION=1.0.0
SERVICE_PORT=9000
```

### 3. Implement Endpoints

Edit `app/main.py` to implement:
- Health checks
- Metadata endpoint
- Your service-specific endpoints

### 4. Create Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s \
    CMD curl -f http://localhost:9000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
```

### 5. Add to Docker Compose

Create a `docker-compose.<service>.yml` file:

```yaml
my-service:
  build:
    context: ./services/my-service
  container_name: nexus-my-service
  ports:
    - "9000:9000"
  environment:
    - SERVICE_NAME=my-service
    - SERVICE_VERSION=1.0.0
  networks:
    - nexus
  restart: unless-stopped
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:9000/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

### 6. Update Gateway

The gateway will auto-discover your service via `/v1/metadata` if configured properly.

For multi-host deployments, register the service in etcd:

```bash
curl -X POST http://etcd:2379/v3/kv/put \
  -H "Content-Type: application/json" \
  -d '{
    "key": "L25leHVzL3NlcnZpY2VzL215LXNlcnZpY2U=",
    "value": "eyJuYW1lIjoibXktc2VydmljZSIsImJhc2VfdXJsIjoiaHR0cDovL215LXNlcnZpY2U6OTAwMCIsIm1ldGFkYXRhX3VybCI6Imh0dHA6Ly9teS1zZXJ2aWNlOjkwMDAvdjEvbWV0YWRhdGEifQ=="
  }'
```

Add backend URL to gateway environment:

```yaml
environment:
  - MY_SERVICE_BASE_URL=http://my-service:9000
```

### 7. Test

```bash
# Build and start
docker compose up -d my-service

# Test health
curl http://localhost:9000/health

# Test readiness
curl http://localhost:9000/readyz

# Test metadata
curl http://localhost:9000/v1/metadata
```

## Service Development Checklist

- [ ] Implements `/health` endpoint
- [ ] Implements `/readyz` endpoint with backend checks
- [ ] Implements `/v1/metadata` with complete schema
- [ ] Follows OpenAI API conventions (where applicable)
- [ ] Includes proper error handling
- [ ] Has structured logging
- [ ] Has health check in Dockerfile
- [ ] Has resource limits defined
- [ ] Has security best practices (non-root user, etc.)
- [ ] Has comprehensive README
- [ ] Has configuration examples
- [ ] Has tests

## Best Practices

### Configuration
- Use environment variables for all config
- Provide sensible defaults
- Validate configuration on startup
- Document all variables in `.env.example`

### Logging
- Use structured logging (JSON)
- Include correlation IDs
- Log errors with stack traces
- Don't log sensitive data

### Error Handling
- Use standard HTTP status codes
- Return error details in response body
- Handle timeouts gracefully
- Fail fast when appropriate

### Security
- Run as non-root user
- Use minimal base images
- Don't expose unnecessary ports
- Validate all inputs
- Sanitize outputs

### Performance
- Implement request timeouts
- Handle backpressure
- Clean up resources
- Use connection pooling

### Testing
- Unit tests for business logic
- Integration tests with real backends
- Health endpoint tests
- Load tests for production readiness

## Examples

See these services for reference implementations:

- `services/gateway/` - Full-featured gateway
- `services/ollama/` - Wrapping an existing service
- `services/images/` - GPU-accelerated service
- `services/tts/` - Audio processing service

## Resources

- [SERVICE_API_SPECIFICATION.md](../../SERVICE_API_SPECIFICATION.md) - API requirements
- [ARCHITECTURE.md](../../ARCHITECTURE.md) - System design
- [Docker Compose docs](https://docs.docker.com/compose/)
- [FastAPI docs](https://fastapi.tiangolo.com/)
