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

### Backend Placement Policy

- Backends that can use Apple Silicon acceleration must run on macOS bare metal (host-native), not in Linux containers.
- Backends that are CPU-only and do not benefit from NVIDIA acceleration should run in containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated backends should run on dedicated Linux/NVIDIA hosts.

### Current Host Inventory (2026-03-02 snapshot)

- `ai2` (macOS Apple Silicon): **512GB unified memory**. Primary host-native accelerator target for `ollama` + `mlx`.
- `ada2` (Linux/NVIDIA): ~31GiB RAM, NVIDIA RTX 6000 Ada (46GB VRAM), currently running heavy CUDA workloads (`heartmula`, `invokeai`).
- `ai1` (Linux/NVIDIA): ~15GiB RAM, NVIDIA RTX 5060 Ti (16GB VRAM), currently running SDXL Turbo workload.

Operational implication:
- Keep latency-sensitive and high-context LLM traffic on `ai2` via host-native MLX/Ollama.
- Keep NVIDIA-centric image/vision/CUDA services on `ada2`/`ai1`.
- Avoid scheduling additional persistent LLM GPU workloads on `ada2`/`ai1` unless capacity is explicitly reclaimed.

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
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d

# Start core + Telegram bot (optional)
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.telegram-bot.yml up -d

# Start core + MLX component (optional)
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.mlx.yml up -d

# Check service health
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml ps

# View gateway logs
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml logs -f gateway

# Stop services
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml down
```

### HTTPS Proxy (nginx: 80 -> 443 redirect)

Generate a local/self-signed cert (or place your real certs at `./.runtime/nginx/certs/fullchain.pem` and `./.runtime/nginx/certs/privkey.pem`):

```bash
./deploy/scripts/generate-nginx-self-signed-cert.sh
```

Start nginx TLS terminator in front of gateway:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.nginx.yml up -d --build
```

### Native Apple Silicon Accelerator Mode (Recommended for Ollama + MLX)

For Apple-accelerated inference, run Ollama/MLX natively on a macOS Apple Silicon host and keep Nexus control-plane services in containers.

```bash
# Containerized control plane only (no ollama/mlx containers)
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d
```

Set these in `.env` so Gateway targets native services:

```bash
# Same-machine macOS host from inside Docker Desktop containers
OLLAMA_BASE_URL=http://host.docker.internal:11434
MLX_BASE_URL=http://host.docker.internal:10240/v1

# Or remote Mac accelerator node
# OLLAMA_BASE_URL=http://mac-accelerator-01:11434
# MLX_BASE_URL=http://mac-accelerator-01:10240/v1
```

Security recommendations for native accelerator hosts:
- Bind native inference services to loopback when possible and front them with a local reverse proxy.
- Enforce IP allowlist and firewall rules so only Gateway hosts can connect.
- Run services under dedicated non-admin users with minimal filesystem permissions.
- Keep model/cache directories scoped to service users and avoid broad host mounts.

macOS helper for MLX allowlisting (port `10240`, defaults: `10.10.22.156`, `172.28.0.0/16`, `127.0.0.1`, `192.168.65.0/24`):

```bash
./deploy/scripts/allowlist-mlx-macos.sh
```

To allow multiple client IPs:

```bash
./deploy/scripts/allowlist-mlx-macos.sh --allow 10.10.22.156 --allow 10.10.22.157
```

### Ollama + MLX Container-to-Bare-Metal Migration Runbook

Use this when moving inference from `docker-compose.ollama.yml` / `docker-compose.mlx.yml` to a macOS Apple Silicon host.

1. On the macOS host, install native services:

```bash
./services/ollama/scripts/install-native-macos.sh --host 127.0.0.1 --port 11434
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

2. Verify native service health on the macOS host:

```bash
curl -fsS http://127.0.0.1:11434/api/version
curl -fsS http://127.0.0.1:10240/v1/models
```

3. In `nexus/.env`, set external/native targets:

```bash
# Same machine (Docker Desktop on macOS)
OLLAMA_BASE_URL=http://host.docker.internal:11434
MLX_BASE_URL=http://host.docker.internal:10240/v1

# Or remote macOS accelerator node
# OLLAMA_BASE_URL=http://<mac-host-or-ip>:11434
# MLX_BASE_URL=http://<mac-host-or-ip>:10240/v1
```

4. Restart Nexus without containerized Ollama/MLX:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --build
```

5. Verify Gateway against external/native backends:

```bash
./deploy/scripts/verify-gateway.sh --external-ollama --external-mlx
```

6. After successful verification, keep `docker-compose.ollama.yml` and `docker-compose.mlx.yml` out of steady-state compose invocations.

### Setup/Install Scripts Reference

These scripts are the current supported setup/install and deployment entrypoints:

- `quickstart.sh`: interactive local bootstrap (recommended for first run)
- `deploy/scripts/preflight-check.sh`: dependency + permission checks
- `deploy/scripts/deploy.sh <dev|prod> <branch>`: host-local deployment
- `deploy/scripts/remote-deploy.sh <dev|prod> <branch> <user@host>`: remote deployment wrapper
- `deploy/scripts/ops-stack.sh [--branch <name>]`: host-local daily ops (`git pull` + restart core stack + verify)
- `deploy/scripts/restart-gateway.sh`: restart/rebuild only Gateway so code/config updates are picked up quickly
- `deploy/scripts/redeploy-tts-shims.sh`: redeploy containerized `pocket_tts` + `luxtts` + `qwen3-tts` and optionally restart Gateway
- `deploy/scripts/seed-tts-refs.sh --source <path>`: seed shared `./.runtime/tts_refs` from local audio files with content-hash dedup
- `deploy/scripts/prewarm-models.sh`: prewarm Ollama models (container or host-native mode)
- `deploy/scripts/prewarm-mlx.sh`: prewarm MLX model runtime (host-native recommended)

Alias-aware prewarm options:

- `deploy/scripts/prewarm-models.sh --from-aliases`: include all `backend=ollama` models from `./.runtime/gateway/config/model_aliases.json`
- `deploy/scripts/prewarm-mlx.sh --from-aliases`: include all `backend=mlx` models from `./.runtime/gateway/config/model_aliases.json`
- `services/ollama/scripts/install-native-macos.sh`: install/manage host-native Ollama (launchd)
- `services/mlx/scripts/install-native-macos.sh`: install/manage host-native MLX (launchd)
- `deploy/scripts/allowlist-mlx-macos.sh`: configure macOS `pf` allowlist for MLX port access
- `deploy/scripts/register-service.sh <name> <base-url> <etcd-url>`: register service metadata in etcd
- `deploy/scripts/list-services.sh <etcd-url>`: list registered services in etcd

### Seed shared TTS refs

Use this utility to populate the shared reference-audio pool used by Gateway and TTS containers (`./.runtime/tts_refs`).

```bash
./deploy/scripts/seed-tts-refs.sh --source /path/to/voice-samples
```

Multiple sources and dry-run preview:

```bash
./deploy/scripts/seed-tts-refs.sh --source /path/a --source /path/b --dry-run
```

The script sanitizes voice IDs from filenames and deduplicates by audio content hash, so re-seeding does not create duplicates.

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
- Gateway model alias config lives at `./.runtime/gateway/config/model_aliases.json`.
- For a practical MLX-fast + Ollama-strong split example, see `services/mlx/README.md`.
- For `ai2` (512GB) model tier recommendations and Linux host placement notes, see `services/mlx/README.md`.

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

### LuxTTS (`services/luxtts/`)
- OpenAI-compatible LuxTTS shim
- Proxy/subprocess runtime modes
- **Port**: 9170

### Qwen3-TTS (`services/qwen3-tts/`)
- OpenAI-compatible Qwen3-TTS shim
- Proxy/subprocess runtime modes
- **Port**: 9175

### Telegram Bot (`services/telegram-bot/`)
- Telegram chat bridge into Gateway endpoints
- Uses `TELEGRAM_TOKEN` and `GATEWAY_BEARER_TOKEN` from `.env`
- Containerized component (no host systemd/launchd required)

### Etcd (`etcd`)
- Service discovery registry for multi-host deployments
- Gateway polls for service base URLs
- **Port**: 2379

Operational scripts and key layout are documented in [docs/ETCD_OPERATIONS.md](docs/ETCD_OPERATIONS.md).

See `services/README.md` for complete service documentation.

## Adding a New Service

1. Create service directory: `services/my-service/`
2. Implement required endpoints:
   - `/health` - Liveness check
   - `/readyz` - Readiness check
   - `/v1/metadata` - Capability advertisement
3. Add Dockerfile
4. Add a new per-component compose file (e.g. `docker-compose.<service>.yml`)
  - Policy: see `COMPOSE_POLICY.md`.
5. Gateway auto-discovers via `/v1/metadata`

See [SERVICE_API_SPECIFICATION.md](SERVICE_API_SPECIFICATION.md) for API requirements.

## Development

### Project Structure

```
nexus/
├── docker-compose.gateway.yml  # Gateway component
├── docker-compose.ollama.yml   # Ollama component
├── docker-compose.etcd.yml     # Etcd component
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
docker compose -f docker-compose.gateway.yml -f docker-compose.gateway.dev.yml up -d

# View logs for specific service
docker compose logs -f ollama

# Restart a service
docker compose restart gateway
```

### CI/CD and Dev Branch Deployments

Temporary status: GitHub Actions build/deploy workflows are currently manual-only (`workflow_dispatch`) until the image upload target/registry configuration is finalized.

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
| launchd/systemd scripts | docker-compose.*.yml | One-file-per-component compose |
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

# Update docker-compose.ollama.yml to use gpus
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
