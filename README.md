# Nexus - Extensible AI-orchestration infrastructure

Nexus is a container-based AI orchestration platform that combines API gateway capabilities with modular AI services. It provides a unified interface for chat, image generation, audio processing, and other AI capabilities.

## Overview

Nexus transforms the monolithic AI infrastructure into a containerized microservices architecture:

- **Container-based**: All services run in Docker containers with isolation
- **API-driven**: Services communicate via standardized REST APIs
- **Discoverable**: Services self-advertise capabilities via `/v1/metadata` endpoints
- **Extensible**: Add new services without modifying gateway code
- **OpenAI-compatible**: Follow industry-standard API conventions
- **Service discovery**: Etcd-backed registry for multi-host routing

## Architecture

```
Client → Gateway → [LLM Services, Image Services, Audio Services, ...]
```

- **Gateway**: Central API gateway (OpenAI-compatible endpoints, authentication, routing)
- **Services**: Independent containerized AI backends (Ollama, InvokeAI, TTS, etc.)
- **Discovery**: Services register capabilities automatically
- **Communication**: HTTP APIs over private Docker network

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design documentation.

## Quick Start

Compose policy: see [COMPOSE_POLICY.md](COMPOSE_POLICY.md) (one compose file per component; use `-f` layering).

### Prerequisites

- Operator environment: **macOS/Linux hosts** with Docker Engine and the `docker compose` plugin
- Development on Windows: use **WSL2 (Ubuntu)** + Docker Desktop WSL integration; run Nexus scripts from within WSL
- Bash + curl + openssl (used by setup scripts)
- (Optional) NVIDIA Container Toolkit for GPU services

### Recommended: Guided Setup Script

Use the interactive installer to (best-effort) install missing prerequisites, run preflight checks, create `.env` (from `.env.example`), and bring the stack up safely:

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
./deploy/scripts/install-host-deps.sh
./quickstart.sh
```

- `install-host-deps.sh` is interactive and installs Docker/Docker Compose (+ optional NVIDIA runtime).
- `quickstart.sh` runs preflight checks, creates `.env`, starts services, and verifies readiness.

For non-interactive environments, use:

```bash
./quickstart.sh --yes
```

The quickstart flow automatically runs `deploy/scripts/preflight-check.sh` and validates key prerequisites before starting containers.

Gateway persistence is stored on the host under `./.runtime/gateway/` and bind-mounted into the container:
- **Read-write data** (SQLite DBs, tool logs, cached UI assets): `./.runtime/gateway/data/` → `/var/lib/gateway/data`
- **Read-only operator config** (model aliases, agent specs, tools registry): `./.runtime/gateway/config/` → `/var/lib/gateway/config`

### Start the Stack

```bash
# Start core services (gateway + ollama + etcd)
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d

# Check service health
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml ps

# View gateway logs
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml logs -f gateway

# Stop services
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml down
```

### Setup/Install Scripts Reference

These scripts are the current supported setup/install and deployment entrypoints:

- `quickstart.sh`: interactive local bootstrap (recommended for first run)
- `deploy/scripts/preflight-check.sh`: dependency + permission checks
- `deploy/scripts/deploy.sh <dev|prod> <branch>`: host-local deployment
- `deploy/scripts/remote-deploy.sh <dev|prod> <branch> <user@host>`: remote deployment wrapper
- `deploy/scripts/register-service.sh <name> <base-url> <etcd-url>`: register service metadata in etcd
- `deploy/scripts/list-services.sh <etcd-url>`: list registered services in etcd

### Access the Gateway

Once running, the gateway is available at:
- **API**: `http://localhost:8800`
- **Health**: `http://localhost:8800/health`
- **Docs**: `http://localhost:8800/docs` (Swagger UI)

### Example Request

```bash
# Chat completion
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'

# List available models
curl http://localhost:8800/v1/models \
  -H "Authorization: Bearer YOUR_TOKEN"

# Generate an image
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "prompt": "A serene mountain landscape",
    "size": "1024x1024"
  }'
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# Gateway configuration
GATEWAY_BEARER_TOKEN=your-secret-token
GATEWAY_PORT=8800

# Enable/disable services
ENABLE_OLLAMA=true
ENABLE_IMAGES=true
ENABLE_AUDIO=true

# Service discovery
ETCD_ENABLED=true
ETCD_URL=http://etcd:2379
ETCD_PREFIX=/nexus/services/
```

See `.env.example` for all available options.

### Service Configuration

- Nexus uses a single env file at `./.env` (created from `./.env.example`).
- Service-specific templates live under `services/<name>/env/*.example`.
- Persistent state and large artifacts live under `./.runtime/` (bind mounts), not Docker named volumes.

## Services

Nexus includes the following services:

### Gateway (`services/gateway/`)
- OpenAI-compatible API gateway
- Request routing and load balancing
- Authentication and authorization
- Service discovery and health monitoring
- **Ports**: 8800 (API), 8801 (observability)

### Ollama (`services/ollama/`)
- Large language model inference
- Supports Llama, Qwen, Mistral, and other models
- Streaming responses
- **Port**: 11434

### Image Generation (`services/images/`)
- Text-to-image generation
- InvokeAI or ComfyUI backend
- SDXL model support
- **Port**: 7860

### TTS (`services/tts/`)
- Text-to-speech synthesis
- Multiple voice options
- Streaming audio output
- **Port**: 9940

### Etcd (`etcd`)
- Service discovery registry for multi-host deployments
- Gateway polls for service base URLs
- **Port**: 2379

See `services/README.md` for complete service documentation.

## Adding a New Service

1. Create service directory: `services/my-service/`
2. Implement required endpoints:
   - `/health` - Liveness check
   - `/readyz` - Readiness check
   - `/v1/metadata` - Capability advertisement
3. Add Dockerfile
4. Add to `docker-compose.yml`
5. Gateway auto-discovers via `/v1/metadata`

See [SERVICE_API_SPECIFICATION.md](SERVICE_API_SPECIFICATION.md) for API requirements.

## Development

### Project Structure

```
nexus/
├── docker-compose.yml          # Service orchestration
├── .env.example                # Configuration template
├── ARCHITECTURE.md             # Design documentation
├── SERVICE_API_SPECIFICATION.md # API standards
├── services/                   # Service implementations
│   ├── gateway/                # API gateway
│   ├── ollama/                 # LLM service
│   ├── images/                 # Image generation
│   ├── tts/                    # Text-to-speech
│   └── template/               # Service template
└── docs/                       # Additional documentation
```

### Running Tests

```bash
# Test gateway
docker compose exec gateway pytest

# Test all services
docker compose exec gateway python tools/verify_gateway.py
```

### Development Mode

```bash
# Start with hot reload
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# View logs for specific service
docker compose logs -f ollama

# Restart a service
docker compose restart gateway
```

### CI/CD and Dev Branch Deployments

See [docs/CI_CD.md](docs/CI_CD.md) for automated build/deploy guidance, secrets handling, and dev branch workflows.
See [docs/INITIAL_ROLLOUT.md](docs/INITIAL_ROLLOUT.md) for first-time rollout order and implicit requirements.

## Replication Plan

See [docs/REPLICATION_PLAN.md](docs/REPLICATION_PLAN.md) for a detailed checklist of what remains to reach gateway/ai-infra parity with the new architecture.

## Deployment

### Local Development
Use `docker compose up` for single-host development.

### Production
- Use `docker compose` for simple production deployments
- For multi-host rollouts, use the per-service manifests in `deploy/` (Docker Compose or nerdctl/containerd)
- Configure resource limits, health checks, and monitoring
- Use TLS/HTTPS termination at load balancer or ingress

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for detailed deployment guides.
See [deploy/README.md](deploy/README.md) for per-service Docker Compose and containerd manifests.
See [docs/CI_CD.md](docs/CI_CD.md) for CI/CD workflows and convenience scripts.
See [docs/DYNAMIC_BACKEND_UI.md](docs/DYNAMIC_BACKEND_UI.md) for descriptor-driven backend UI composition.

## Bootstrapping Path (Recommended)

1. Start with a **single-host** deployment (gateway + one backend).
2. Validate `/health`, `/readyz`, and `/v1/metadata` for the backend.
3. Confirm **OpenAI-compatible** requests via the gateway.
4. Move one backend to a **remote host**, update its base URL at runtime.
5. Add **network security** (VPN/private network, mTLS, firewall rules).

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for multi-host guidance.

## Monitoring

### Health Checks
All services expose health endpoints:
- `/health` - Liveness (is the process running?)
- `/readyz` - Readiness (can it handle requests?)

### Metrics
Prometheus-compatible metrics at `/metrics`:
```bash
curl http://localhost:8800/metrics
```

### Logs
Structured JSON logging with correlation IDs:
```bash
# View all logs
docker compose logs -f

# View gateway logs
docker compose logs -f gateway

# View logs for specific request
docker compose logs gateway | grep req_abc123
```

## Migration from ai-infra

Nexus replaces the host-based `ai-infra` deployment with containers:

| Old (ai-infra) | New (Nexus) | Notes |
|----------------|-------------|-------|
| launchd/systemd scripts | docker-compose.yml | Unified orchestration |
| Manual host setup | Dockerfile per service | Reproducible environments |
| Host networking | Docker networks | Isolated networking |
| `/var/lib/gateway` | `./.runtime/gateway/*` bind mounts | Persistent data + operator config |
| SSH + manual deploys | `docker compose up` | One command deploys |

See [docs/MIGRATION.md](docs/MIGRATION.md) for the scripted migration workflow (`deploy/scripts/migrate-from-ai-infra.sh`) and detailed manual migration guide.

## Troubleshooting

### Service won't start
```bash
# Check logs
docker compose logs <service-name>

# Check health
curl http://localhost:8800/health
```

### Can't connect to backend
```bash
# Verify service is running
docker compose ps

# Check network connectivity
docker compose exec gateway curl http://ollama:11434/health
```

### GPU not detected
```bash
# Verify NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# Update docker-compose.yml to use gpus
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for additional operational guidance.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Implement your changes
4. Add tests
5. Submit a pull request

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

See [LICENSE](LICENSE) file for details.

## Credits

Nexus builds on:
- Original gateway implementation (gevanoff/gateway)
- Infrastructure patterns (gevanoff/ai-infra)
- OpenAI API standards
- Docker and container ecosystem

## Support

- **Issues**: [GitHub Issues](https://github.com/gevanoff/nexus/issues)
- **Discussions**: [GitHub Discussions](https://github.com/gevanoff/nexus/discussions)
- **Documentation**: [docs/](docs/)
