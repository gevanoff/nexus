# Nexus Architecture

Nexus is an extensible AI-orchestration infrastructure built on containerized microservices with standardized API conventions.

## Design Principles

### 1. Container-Based Architecture
- All services run in isolated Docker containers with limited host access
- Container orchestration via Docker Compose
- Each service has minimal dependencies and a well-defined runtime environment
- Resource limits and security boundaries enforced at the container level

### 2. Discrete Functions
- Services are single-purpose and independently deployable
- Clear separation of concerns (gateway, LLM inference, image generation, audio, etc.)
- Each service can be scaled independently
- Services communicate only through well-defined APIs

### 3. Standardized APIs
- All services follow AI industry standard conventions (OpenAI-compatible where applicable)
- Common endpoint patterns across all services:
  - `/health` - Liveness check
  - `/readyz` - Readiness check  
  - `/v1/metadata` - Service discovery and capability advertisement
- Consistent request/response formats
- Standardized error handling

### 4. Service Discovery
- Every service exposes a `/v1/metadata` endpoint describing:
  - Service capabilities and supported operations
  - Available endpoints and their schemas
  - Configuration options and UI controls
  - Resource requirements and limits
- Gateway and clients can dynamically discover and integrate new services
- No hardcoded service configurations required
- Backends may additionally expose `/v1/descriptor` for enhanced contract metadata (response types, UI placement hints, and endpoint affordances)

## Core Components

### Gateway Service
Central API gateway that:
- Exposes unified OpenAI-compatible endpoints to clients
- Routes requests to appropriate backend services
- Handles authentication and authorization
- Provides request logging and metrics
- Implements policy-based routing
- Manages tool bus for agent operations
- Builds backend/resource UI state from static backend config, registry records, lifecycle metadata, health probes, and model aliases
- Serves the current Nexus web UI bundle, including Chat, Resources, Coding Workspaces, Scheduled Tasks, and focused media/service UIs
- Persists UI users/settings/API keys, coding workspace metadata, agent run logs, scheduled-task state, and generated UI artifacts under `/var/lib/gateway/data`

### UI Layer
The current production UI is gateway-served and same-origin with the API. It includes Chat (`/ui`), Resources, Coding Workspaces, Scheduled Tasks, image, music, video, OCR, TTS, voice cloning, PersonaPlex, and admin user management. The focused UIs share top navigation, Apps, Settings, Resources, Refresh, and API-key status affordances.

A separate UI container remains a possible future hardening/scaling step, but the current supported operator path is the gateway-served UI.

### LLM Services
Language model inference engines:
- Ollama for general-purpose models, usually host-native on Apple Silicon when used
- MLX for Apple Silicon optimization, usually host-native on `ai2`
- OpenAI-compatible API interface
- Model management and loading
- Streaming response support

### Image Generation Services
Text-to-image and image manipulation services:
- InvokeAI for SDXL models
- ComfyUI support
- OpenAI-compatible `/v1/images/generations` endpoint
- Concurrent request management

### Audio Services
Speech and audio processing:
- Text-to-Speech (TTS) backends
- Automatic Speech Recognition (ASR)
- Music generation (HeartMula)
- OpenAI-compatible audio endpoints

### Specialized Services
Domain-specific capabilities:
- OCR (optical character recognition)
- Video generation
- Custom tool implementations
- Coding workspaces and scheduled agent tasks

## Service Communication

### Network Architecture
```
┌─────────────────────────────────────────────────┐
│                   Client Layer                   │
│  (External clients, web UI, API consumers)      │
└───────────────────┬─────────────────────────────┘
                    │ HTTPS
                    │ Bearer Token Auth
┌───────────────────▼─────────────────────────────┐
│              Gateway Service                     │
│  • Authentication & Authorization                │
│  • Request Routing & Load Balancing             │
│  • Policy Enforcement                           │
│  • Service Discovery                            │
└────┬────────┬────────┬────────┬─────────────────┘
     │        │        │        │
     │ HTTP   │ HTTP   │ HTTP   │ HTTP
     │        │        │        │
┌────▼────┐ ┌─▼──────┐ ┌──▼────┐ ┌─▼──────────┐
│   LLM   │ │ Images │ │ Audio │ │ Specialized│
│ Services│ │Services│ │Services│ │  Services  │
│         │ │        │ │        │ │            │
│• Ollama │ │•Invoke │ │• TTS   │ │• OCR       │
│• MLX    │ │• Comfy │ │• ASR   │ │• Video Gen │
└─────────┘ └────────┘ └────────┘ └────────────┘
```

### Multi-Host Deployment Model

Nexus is designed to run in a multi-host environment where containers are distributed across multiple machines. The gateway remains the primary ingress point, while backend services may live on separate hosts. In this model:

- **Gateway routing targets remote backends** via hostnames/IPs on a private network or VPN.
- **Service discovery** can start as static configuration (env or config file) and later evolve to a registry (Consul/etcd).
- **Trust boundaries** should be explicit: prefer mTLS for internal service calls if networks are shared.
- **Latency-aware routing** may be required to keep inference close to data or to prioritize specific GPU hosts.

Example topology (illustrative, not prescriptive):

```
Client → Gateway (ai2) → Ollama (ai1) → Images (ada2)
```

Key considerations:

- **Network overlay**: WireGuard/Tailscale or a VPC/VLAN to allow stable service addressing across hosts.
- **Firewalling**: restrict backend ports to trusted hosts only; expose the gateway publicly.
- **Configuration**: keep host assignments outside the repo (e.g., via runtime config or environment overrides).
- **Observability**: centralize logs/metrics with correlation IDs across hosts.

### Service Communication Pattern
1. **Client → Gateway**: HTTPS with bearer token authentication
2. **Gateway → Services**: HTTP within Docker network (internal, no auth required)
3. **Service Discovery**: Gateway reads static config, polls etcd registry records, and queries `/v1/metadata`/`/v1/descriptor` where available
4. **Health Monitoring**: Gateway polls `/health` and `/readyz` endpoints

## Data Flow

### Request Processing
1. Client sends request to gateway
2. Gateway validates authentication
3. Gateway applies request policies and guards
4. Gateway routes to appropriate service(s) via service discovery
5. Service processes request and returns response
6. Gateway adds correlation headers and logs
7. Response returned to client

### Streaming Responses
1. Gateway establishes connection to backend service
2. Backend streams chunks as they're generated
3. Gateway wraps stream with instrumentation
4. Client receives Server-Sent Events (SSE) stream
5. Metrics collected on stream completion

## Security Model

### Container Isolation
- Services run as non-root users
- Read-only root filesystems where possible
- Minimal base images (distroless or alpine)
- No unnecessary capabilities
- Resource limits enforced

### Network Security
- Services not directly accessible from outside Docker network
- Gateway is the only externally-exposed service
- Internal service-to-service communication over private network
- No credential sharing between services

### Authentication & Authorization
- Bearer token authentication at gateway level
- Optional per-token policies (rate limits, feature access)
- IP allowlisting and proxy trust settings for UI access
- UI username/password login and browser sessions
- Per-user API keys, preferences, GitHub token storage for coding workspaces, and preferred coding model settings
- Static bearer token fallback for automation and recovery

## Configuration Management

### Environment-Based Config
- Each service configured via environment variables
- Sensitive values via Docker secrets or environment files
- Configuration validation on startup
- Sane defaults for all settings

### Service Registration
- Services self-register capabilities via `/v1/metadata`
- Gateway discovers services by querying metadata endpoints
- Dynamic service addition without gateway restarts
- Capability-based routing (e.g., only route image requests to services advertising `domains: ["image"]`)
- **Etcd-backed discovery**: services register their base URLs in etcd (`/nexus/services/<name>`), and the gateway polls etcd for updates

### Agent Runtime And Tools
- Agent specs are loaded from `AGENT_SPECS_PATH` and can set model aliases, tool tier, tool IO budgets, and explicit tool allowlists.
- Tool access is tiered. Tier 0 is read/browse/scheduling oriented; tier 1 adds memory and file-write tools; tier 2 adds constrained shell execution.
- The tools bus exposes tool metadata and execution through `/v1/tools` and includes web browsing, local file reads/search, memory operations, HeartMula music generation, and scheduled-task create/list/cancel tools.
- Agent runs are persisted under `AGENT_RUNS_LOG_DIR` or `AGENT_RUNS_LOG_PATH`.
- Scheduled tasks are persisted in `AGENT_TASKS_DB_PATH` and executed by a gateway scheduler loop. Current scheduled task UI/API supports LLM/text tasks; coder, app, multi-model, image, music, and video task types are reserved in the UI/API shape for future runners.

### Coding Workspaces
- Coding tasks create isolated git clones under `CODING_WORKSPACE_ROOT`.
- The workspace API exposes bounded tree/file/search/patch/command/git operations and blocks high-risk git subcommands.
- Autonomous coding runs can be started, paused, resumed after gateway restarts at the metadata level, and steered with workspace messages.
- Successful or in-progress agent work can create local checkpoint commits; pushing and draft PR creation remain explicit user actions.
- Git credentials are per-user preferences when the browser/API-key user is authenticated. `CODING_GIT_TOKEN` is only a fallback for static-bearer automation.

## Observability

### Metrics
- Prometheus-compatible metrics endpoint (`/metrics`)
- Request counts, latencies, error rates
- Resource utilization (memory, CPU)
- Per-service and per-endpoint granularity

### Logging
- Structured JSON logging
- Request correlation IDs
- Per-request instrumentation
- Streaming metrics capture
- Optional JSONL request logs for replay

### Health Checks
- Liveness: `/health` - Is the service process running?
- Readiness: `/readyz` - Can the service handle requests?
- Upstream health: Gateway monitors backend health
- Automatic retry and failover on unhealthy backends

## Deployment Model

### Local Development
```bash
docker compose up
```
Single-command startup of full stack for development.

### Production Deployment
- Docker Compose for simpler deployments
- For multi-host rollouts, use the per-service manifests under `deploy/` (Docker Compose or nerdctl/containerd)
- Service-specific resource requirements in metadata
- Horizontal scaling for stateless services

## Extension Points

### Adding New Services
1. Implement service with required endpoints (`/health`, `/readyz`, `/v1/metadata`)
2. Follow OpenAI-compatible API patterns where applicable
3. Add a new per-component compose file (e.g. `docker-compose.<service>.yml`)
4. Gateway automatically discovers via `/v1/metadata`
5. No gateway code changes required

### Custom Tools
- Implement tool following tools bus specification
- Register in gateway's tool registry
- Grant through an agent spec tier/allowlist before expecting it to be available at runtime

### Backend Models
- Add new model to service's model manifest
- Service downloads/loads model on startup
- Model available via gateway routing

## Migration from ai-infra

The original `ai-infra` repository used macOS launchd and Linux systemd for service management. Nexus containerizes these services:

| ai-infra Service | Nexus Container | Notes |
|-----------------|-----------------|-------|
| gateway (launchd) | gateway | FastAPI app, externally exposed |
| ollama (launchd/systemd) | ollama | Official Ollama Docker image |
| mlx (launchd) | mlx | Custom container with MLX |
| invokeai (systemd) | invokeai | InvokeAI with model persistence |
| heartmula (systemd) | heartmula | Music generation service |
| pocket-tts (launchd/systemd) | tts | TTS service shim |

Key differences:
- **Containerized control plane**: Gateway, etcd, lifecycle-manager, nginx, Telegram bot, and many shims run through Compose.
- **Host-native accelerators where needed**: MLX and sometimes Ollama run on macOS bare metal for Apple Silicon acceleration; CUDA services run on Linux/NVIDIA hosts.
- **Unified networking**: Gateway reaches backends through Compose DNS, registered service URLs, or host aliases rendered from private runtime env.
- **Reproducible deploys**: Code changes should be committed/pushed and pulled onto hosts through deployment scripts rather than live edits.
- **Explicit host access**: Services cannot access host filesystems without declared volumes; lifecycle operations use dedicated SSH identity/mounts.

## Future Enhancements

- [ ] Kubernetes operator for auto-scaling
- [ ] Service mesh integration (Istio/Linkerd)
- [ ] Distributed tracing (OpenTelemetry)
- [ ] Multi-region deployment support
- [ ] GPU resource management
- [ ] Model caching and sharing between services
- [ ] Split UI into a separate deployable service if gateway-served UI becomes a scaling or security bottleneck
- [ ] Extend scheduled tasks to coder, app, multi-model, image, music, and video runners
