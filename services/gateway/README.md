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
- **User API Keys**: Per-user bearer keys that can be created/revoked from UI settings
- **Focused UIs**: Gateway-served Chat, Resources, Coding Workspaces, Scheduled Tasks, and media/service UIs
- **Agent Runtime**: Tiered tools bus, persistent agent run logs, scheduled LLM tasks, and multi-backend coordination
- **Coding Workspaces**: Isolated git clones with scoped file/command/git APIs, agent runs, local checkpoint commits, pushes, and draft PRs
- **Prompt Prefix Telemetry**: Optional structured latency logs with deterministic prompt-prefix fingerprinting for cache-reuse analysis on coding workloads

## Endpoints

### Core Endpoints

- `GET /health` - Liveness check
- `GET /readyz` - Readiness check with backend validation
- `GET /v1/gateway/status` - Gateway backend health/admission status
- `GET /v1/registry` - Gateway view of registered service records

### OpenAI-Compatible Endpoints

- `GET /v1/models` - List available models
- `POST /v1/chat/completions` - Create chat completion (streaming supported)
- `POST /v1/completions` - Create text completion
- `POST /v1/embeddings` - Create embeddings
- `POST /v1/rerank` - Rerank documents
- `POST /v1/responses` - Responses-compatible wrapper
- `POST /v1/images/generations` - Image generation
- `POST /v1/images/edits` - Image editing
- `POST /v1/audio/speech` - Text-to-speech
- `POST /v1/audio/transcriptions` - Speech-to-text
- `POST /v1/music/generations` - Music generation

### Agent, Tool, Memory, And Coding Endpoints

- `POST /v1/agent/run` - Run one configured agent spec
- `POST /v1/agent/coordinate` - Fan out to several model aliases and synthesize
- `GET /v1/agent/replay/{run_id}` - Replay a persisted agent run
- `GET /v1/tools` / `POST /v1/tools/{name}` - Inspect and execute registered tools, subject to auth/policy
- `POST /v1/memory/*` and `GET /v1/memory/*` - Persistent memory operations
- `/v1/coding/*` - Isolated coding workspace API for tree/file/search/patch/command/git/agent operations
- `/api/v1/*` - Scoped personal-token API for external agents managing coding workspaces, tasks, execution, and artifacts

### Backend Status/UI Helper Endpoints

- `GET /ui/api/backend_status` - Backend/resource status used by Chat and Resources
- `GET /ui/api/lifecycle/status` - Lifecycle-manager service state
- `POST /ui/api/lifecycle/ensure` - Ask lifecycle-manager to activate a backend
- `POST /ui/api/lifecycle/action` - Ask lifecycle-manager to perform a lifecycle action
- `GET /ui/api/models` - UI-friendly model list with cached backend probing
- `GET /ui/api/image/catalog` and `/ui/api/{tts,music,video,ocr}/backends` - Focused UI backend catalogs

### Gateway-Served UI Routes

- `/ui` - Main Chat UI
- `/ui/login` - Login/API-key session entry point
- `/ui/resources` - Backend/resource status and lifecycle controls
- `/ui/coding` - Coding Workspaces
- `/ui/tasks` - Scheduled Tasks
- `/ui/image`, `/ui/music`, `/ui/video`, `/ui/ocr`, `/ui/tts`, `/ui/voice-clone`, `/ui/personaplex` - Focused capability UIs
- `/ui/admin/users` - Admin user management

The focused UIs share top navigation and include Back to Chat, Refresh, Resources, Apps, Settings, and API-key status where applicable.

### User API Key Endpoints (UI Authenticated)

- `GET /ui/api/user/api-keys` - List API keys for the authenticated user
- `POST /ui/api/user/api-keys` - Create a new API key for the authenticated user
- `DELETE /ui/api/user/api-keys/{key_id}` - Revoke one API key for the authenticated user

Notes:
- Raw API key values are only returned once at key creation.
- API keys can be used as `Authorization: Bearer <api-key>` for gateway API calls.
- UI browser login/session flow remains supported alongside API keys.
- The `/ui/login` page also accepts a personal API key and stores it client-side for browser-based UI/API use.

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
OLLAMA_BASE_URL=
MLX_BASE_URL=http://host.docker.internal:10240/v1
SDXL_TURBO_BASE_URL=http://sdxl-turbo:9050
INVOKEAI_BASE_URL=http://invokeai:9090
LIGHTON_OCR_API_BASE_URL=http://lighton-ocr:9155
HEARTMULA_BASE_URL=http://heartmula:9185
DEFAULT_BACKEND=local_mlx
EMBEDDINGS_BACKEND=local_mlx

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
AGENT_RUNS_LOG_DIR=/var/lib/gateway/data/agent
AGENT_TASKS_DB_PATH=/var/lib/gateway/data/agent/tasks.sqlite
CODING_WORKSPACE_ROOT=/var/lib/gateway/data/coding/workspaces
CODING_TASKS_DIR=/var/lib/gateway/data/coding/tasks

# Prompt-prefix latency telemetry
PROMPT_PREFIX_TELEMETRY_ENABLED=true
PROMPT_PREFIX_OBSERVATION_CACHE_SIZE=2048

# Operator config (mounted read-only from the host)
MODEL_ALIASES_PATH=/var/lib/gateway/config/model_aliases.json
AGENT_SPECS_PATH=/var/lib/gateway/config/agent_specs.json
TOOLS_REGISTRY_PATH=/var/lib/gateway/config/tools_registry.json
TRANSCRIPTION_BACKEND_CLASS=local_mlx
TRANSCRIPTION_MODEL=
```

### Persistence Layout (Host ↔ Container)

Nexus keeps state and large artifacts on the host under `nexus/.runtime/`.

- RW data: `nexus/.runtime/gateway/data/` → `/var/lib/gateway/data`
- RO operator config: `nexus/.runtime/gateway/config/` → `/var/lib/gateway/config`

Config files are seeded (once) by the setup scripts:
- `tools_registry.json`
- `model_aliases.json`
- `agent_specs.json`

Runtime data files/directories are owned by the gateway, not by git:
- `users.sqlite`: users, password hashes, settings, per-user API key hashes, and hidden user preferences such as saved GitHub tokens.
- `agent/`: per-run agent logs and scheduled-task database.
- `coding/`: coding workspace task metadata and isolated clones.
- `ui_images`, `ui_files`, `ui_audio`, `ui_chats`: generated UI artifacts and chat/conversation history.

Alias precedence note:
- If `MODEL_ALIASES_PATH` is set, that file is authoritative.
- The packaged `services/gateway/app/model_aliases.json` is only used as a fallback when no explicit `MODEL_ALIASES_PATH` is configured.
- If the explicit runtime file is missing or unreadable, Gateway now logs that condition and reports it via `/ui/api/backend_status` instead of silently using the packaged file.

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
  -e MLX_BASE_URL=http://host.docker.internal:10240/v1 \
  -e DEFAULT_BACKEND=local_mlx \
  nexus-gateway
```

### Docker Compose

The gateway is configured in `docker-compose.gateway.yml`. Start with:

```bash
docker compose up gateway
```

### Local Development

The gateway source is vendored into this repo under `services/gateway/app` and `services/gateway/tools`.

### GLM-5.2 Latency Benchmark

Use the dedicated benchmark helper to compare cold and warm prompt-prefix behavior:

```bash
python services/gateway/tools/benchmark_glm52_latency.py \
  --base-url http://127.0.0.1:8800/v1 \
  --model glm-5.2 \
  --api-key "$NEXUS_API_KEY"
```

The script emits JSON lines per scenario and a final summary line. By default it runs three inspectable cases:

- `cold_long_prompt`
- `same_prefix_new_user`
- `continuation_follow_up`

Each output row includes prompt-prefix hash, prefix length, TTFT, total latency, output token estimate, and decode throughput.

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

### Continue.dev

Nexus supports Continue.dev as an OpenAI-compatible provider, including OpenAI-style
tool-calling request and message shapes used by `capabilities: [tool_use]`.
The gateway prefers compatibility and graceful degradation over strict rejection.

Gateway-side execution is available through `x_nexus.tool_execution_mode=gateway_exec` for approved, bounded Nexus tools. Continue and external agents retain `client_exec`, which returns normalized tool calls without executing them. See `docs/TOOL_CALLING.md` for toolsets, policy, provider configuration, and smoke tests; surrounding OpenAI request, response, and message shapes remain compatible.

Minimal Continue `config.yaml` example:

```yaml
name: Nexus Minimal
version: 1.0.0
schema: v1
models:
  - name: Nexus Fast
    provider: openai
    model: fast
    apiBase: http://HOST:PORT/v1
    apiKey: TOKEN
    requestOptions:
      headers:
        Authorization: Bearer TOKEN
    roles:
      - chat
      - edit
      - apply
    capabilities:
      - tool_use
    defaultCompletionOptions:
      temperature: 0.2
      maxTokens: 4096
context:
  - provider: code
  - provider: file
  - provider: diff
```

Compatibility notes:

- `/v1/chat/completions` and `/v1/responses` accept OpenAI-style `tools`, `tool_choice`, `parallel_tool_calls`, assistant `tool_calls`, and `role: "tool"` messages.
- `/v1/chat/completions` supports optional Gateway execution and auto-injected `core`, `repo`, and `ops` tools; `/v1/responses` rejects Gateway execution explicitly instead of dropping it.
- `GET /v1/tool-calling/diagnostics` reports alias/provider parser and execution capabilities.
- Unknown extra OpenAI-compatible fields are accepted unless they create a concrete validation, security, or routing problem.
- If a selected alias or backend does not support native tool calling, Nexus strips tool fields and answers normally unless the request explicitly requires tools, such as `tool_choice: "required"`.
- Validation failures return OpenAI-style JSON errors instead of empty-body `400` responses.

### MLX Image Generation

Use `backend_class: "local_mlx"` plus an image-generation model ID exposed by your
native MLX config.

```bash
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "backend_class": "local_mlx",
    "model": "flux2-klein-4b",
    "prompt": "A blueprint-style schematic of a compact weather station",
    "size": "1024x1024"
  }'
```

### MLX Image Editing

```bash
curl -X POST http://localhost:8800/v1/images/edits \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F backend_class=local_mlx \
  -F model=qwen-image-edit \
  -F prompt='Replace the cloudy sky with a sunset' \
  -F image=@./input.png
```

### MLX Whisper Transcription

```bash
curl -X POST http://localhost:8800/v1/audio/transcriptions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F backend_class=local_mlx \
  -F model=mlx-community/whisper-large-v3-mlx \
  -F file=@./sample.wav
```

### Multi-Backend Coordinator

The coordinator fans a request out to multiple aliases/backends, then synthesizes
the result with a chosen model.

For coding requests, the recommended pattern is:
- primary `coder` on the local MLX host
- secondary `coder-stackrot` / `coder-ada2` as independent cross-checks
- synthesis back onto `default` or `coder`

```bash
curl -X POST http://localhost:8800/v1/agent/coordinate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "input": "Review this migration plan and identify bugs or rollback risks.",
    "participants": ["coder", "coder-stackrot", "coder-ada2"],
    "synthesizer": "default"
}'
```

### Coding Workspaces

The gateway can create isolated git clones for local coding agents:

- browser UI: `https://<gateway>/ui/coding`
- bearer API: `/v1/coding/*`

Typical API flow:

```bash
curl -X POST http://localhost:8800/v1/coding/tasks \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"prompt":"Update gateway docs","base_branch":"main"}'
```

Each task gets a fresh branch under `CODING_WORKSPACE_ROOT`. File reads/writes,
commands, diffs, commits, pushes, and draft PR creation stay scoped to that task
clone. Configure allowed repositories with `CODING_ALLOWED_REPOS`; each user
stores their GitHub token and preferred coding model in User Settings. Tokens are
used through a temporary askpass helper and are not returned by the settings API
after save.

The same page can start autonomous coding runs. `POST /v1/coding/runs` creates
a workspace and starts the background agent for the prompt; existing workspaces
can be started with `POST /v1/coding/tasks/{task_id}/agent-run` and paused with
`POST /v1/coding/tasks/{task_id}/agent-pause` (`agent-stop` remains a
backwards-compatible alias). Agent progress is persisted on
the task under `task.agent.events`. The agent may inspect/search/read/write
workspace files and run the configured allowlisted commands. Push and PR
creation remain explicit approval steps; successful runs can optionally make a
local commit. Coding runs can also be steered after creation with workspace
messages (`POST /v1/coding/tasks/{task_id}/messages` or the UI chat controls),
so one workspace/branch can receive additional requests after the first run.

#### Agent API v1

External agents should use `/api/v1` rather than the UI-oriented routes. This
API uses personal access tokens generated in User Settings. New tokens use the
`nxs_pat_` prefix; existing hashed personal keys remain valid. Cluster-wide
static gateway bearer tokens are intentionally rejected by this API.

The default token scopes are `workspaces:read`, `workspaces:write`,
`tasks:read`, `tasks:write`, `execute`, `artifacts:read`, and
`artifacts:write`. A key policy can provide a smaller `scopes` list and an
optional `workspace_id` binding. Every workspace request also enforces the
owning user ID.

```bash
# Create a workspace using the configured default repository.
curl -X POST http://localhost:8800/api/v1/workspaces \
  -H "Authorization: Bearer nxs_pat_REDACTED" \
  -H "Content-Type: application/json" \
  -d '{"name":"Gateway update","description":"Implement and verify the change"}'

# Start it before direct command or code execution.
curl -X POST http://localhost:8800/api/v1/workspaces/WORKSPACE_ID/start \
  -H "Authorization: Bearer nxs_pat_REDACTED"
```

The API provides workspace lifecycle routes, nested task CRUD/retry routes,
allowlisted command or Python/JavaScript execution, and multipart artifact
upload/download. List routes use opaque cursor pagination. All failures use the
same `error.code`, `error.message`, `error.request_id`, and `error.details`
envelope. The authenticated OpenAPI 3.0 document is available from
`GET /api/v1/schema`; only `GET /api/v1/health` is unauthenticated.

Local models can use the same service layer through the built-in
`nexus_agent_api` function in the `workspace` toolset. The function accepts an
`operation`, nullable `workspace_id` and `task_id`, and a `parameters` object.
It supports workspace lifecycle, task CRUD/retry, execution, and bounded
base64 artifact upload/download. For example:

```json
{
  "operation": "create_task",
  "workspace_id": "code_abcdef123456",
  "task_id": null,
  "parameters": {
    "instruction": "Add regression coverage for the gateway change",
    "priority": "high",
    "max_retries": 2
  }
}
```

The tool never accepts a token as a model argument. Gateway execution passes
the authenticated caller internally: personal tokens retain their exact scopes
and optional workspace binding, while Chat UI sessions act only as their logged
in user. Static service bearer tokens and unauthenticated scheduled runs cannot
use the tool. The default gateway-exec toolsets include `workspace`; global
`client_exec` versus `gateway_exec` behavior is otherwise unchanged.

### Scheduled Tasks

The gateway can run durable scheduled LLM tasks:

- browser UI: `https://<gateway>/ui/tasks`
- UI API: `/ui/api/agent-tasks/*`
- agent tools: `agent_task_create`, `agent_task_list`, `agent_task_cancel`

Supported schedule modes:
- timer (`delay_seconds`)
- exact run time (`run_at`)
- fixed interval (`interval_seconds`)
- cron (`cron`, five-field minute/hour/day/month/weekday)

The task UI lets a user choose the LLM model alias, tool tier, explicit tool
allowlist, max runs, and schedule. Results are shown with
per-run status/output. Current scheduled tasks are LLM/text tasks; the API/UI
shape reserves task types for future coder, app, multi-model, image, music, and
video runners.

### Service Discovery

```bash
curl http://localhost:8800/v1/metadata
```

### Backend Status

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8800/ui/api/backend_status
```

## Implementation

Nexus builds and runs the full gateway implementation from `services/gateway/`.
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

## TTS Voice Workflow

- Use `/ui/voice-clone` to upload or record reusable voice references.
- Saved voices are stored in the shared `./.runtime/tts_refs` library used by Gateway and LuxTTS.
- For LuxTTS, reference clips of roughly 5-15 seconds work better than very short samples; clips under about 2 seconds can fail conditioning or sound generic.
- Large uploads can still be rejected by the active nginx/reverse-proxy body limit before Gateway receives them.
- Use `/ui/tts` with the `luxtts` backend to generate speech from those saved voices.
- The TTS voice picker groups LuxTTS voices into native voices, cloned voices, and shared refs.

## UI Notes

- Chat history is persisted under `UI_CHAT_DIR` and exposed through `/ui/api/conversations/*`.
- Backend/resource status is intentionally summarized in Chat and expanded in `/ui/resources`.
- The Resources page is the canonical place for backend host/resource details and lifecycle activation/deactivation controls.

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

1. Add the route to the appropriate `app/*_routes.py` module.
2. Include the router from `app/main.py` if it is a new route module.
3. Add to metadata/descriptor output if it is part of the public service contract.
4. Update UI/API docs and tests or smoke coverage.

### Integrating New Backend

1. Add environment variable for backend URL
2. Add backend config/aliases in `app/backends.py` or runtime config as appropriate
3. Add health/readiness behavior and lifecycle metadata where applicable
4. Add routing logic or descriptor handling
5. Update metadata to advertise new capabilities
6. Register the service in etcd/topology if it is a multi-host backend

## Testing

```bash
# Run tests
pytest

# Test with real backend
pytest --backend-url http://ollama:11434
```

## License

See main repository LICENSE file.
