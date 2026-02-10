# Service API Specification

This document defines the standard API contract that all Nexus services must implement.

## Overview

All services in the Nexus infrastructure must expose a common set of endpoints for:
- **Health monitoring**: Liveness and readiness checks
- **Service discovery**: Capability advertisement via metadata endpoint
- **Operational APIs**: Domain-specific functionality following AI industry standards

Nexus also supports an **etcd service registry** for multi-host deployments. Services should register their base URLs under `/nexus/services/<service-name>` so the gateway can discover remote backends.

Example registry payload (stored as JSON value in etcd):

```json
{
  "name": "ollama",
  "base_url": "http://ollama:11434",
  "metadata_url": "http://ollama:11434/v1/metadata"
}
```

## Required Endpoints

### 1. Health Check (`/health`)

**Purpose**: Liveness probe - indicates if the service process is running.

**Method**: `GET`

**Response**: 
- **200 OK**: Service is alive
- **Any error**: Service is dead/unresponsive

**Response Body** (optional):
```json
{
  "status": "ok"
}
```

**Implementation Notes**:
- Should return quickly (< 100ms)
- Should NOT depend on backend availability
- Should only check if the service process itself is functional
- Used by orchestrators for restart decisions

**Example**:
```bash
curl http://service:8080/health
# => {"status": "ok"}
```

### 2. Readiness Check (`/readyz`)

**Purpose**: Readiness probe - indicates if the service can handle requests.

**Method**: `GET`

**Response**:
- **200 OK**: Service is ready to accept traffic
- **503 Service Unavailable**: Service is alive but not ready
- **Any other error**: Service has a problem

**Response Body**:
```json
{
  "status": "ready",
  "checks": {
    "backend": "ok",
    "model_loaded": true
  }
}
```

**Implementation Notes**:
- Should verify backend connectivity (if applicable)
- Should check critical dependencies (models loaded, database connected, etc.)
- May take longer than `/health` but should complete in < 5s
- Used by load balancers and gateways for routing decisions

**Example**:
```bash
curl http://service:8080/readyz
# => {"status": "ready", "checks": {"backend": "ok"}}
```

### 3. Metadata Endpoint (`/v1/metadata`)

**Purpose**: Service discovery and capability advertisement.

**Method**: `GET`

**Response**: `200 OK` with JSON metadata

**Response Schema** (v1):
```json
{
  "schema_version": "v1",
  "service": {
    "name": "string (required)",
    "version": "string (recommended)",
    "description": "string (recommended)"
  },
  "backend": {
    "name": "string (optional)",
    "vendor": "string (optional)",
    "base_url": "string (optional)"
  },
  "endpoints": [
    {
      "path": "string (required)",
      "method": "string (required)",
      "operation_id": "string (required)",
      "summary": "string (optional)",
      "request_schema": "url (optional)",
      "response_schema": "url (optional)"
    }
  ],
  "capabilities": {
    "domains": ["array of strings (recommended)"],
    "modalities": ["array of strings (recommended)"],
    "streaming": "boolean (optional)",
    "max_concurrency": "integer (optional)"
  },
  "ui": {
    "options": [
      {
        "key": "string (required)",
        "label": "string (required)",
        "type": "string (required)",
        "default": "any (optional)",
        "values": ["array (optional, for enum type)"],
        "min": "number (optional, for numeric types)",
        "max": "number (optional, for numeric types)",
        "description": "string (optional)"
      }
    ]
  },
  "resources": {
    "cpu": "string (optional, e.g., '2')",
    "memory": "string (optional, e.g., '4Gi')",
    "gpu": "string (optional, e.g., 'nvidia-gpu')",
    "gpu_memory": "string (optional, e.g., '16Gi')"
  }
}
```

### 4. Enhanced Descriptor Endpoint (`/v1/descriptor`) (recommended)

**Purpose**: Advertise endpoint contracts, response types, and UI placement hints for dynamic gateway-driven UI generation.

**Method**: `GET`

**Response**: `200 OK` with JSON descriptor

**Descriptor Extensions**:

- `response_types`: media types by mode (e.g., JSON, SSE)
- `ui_navigation`: placement hints for where this backend appears in the unified UI

**Example**:

```json
{
  "schema_version": "v1",
  "service": {"name": "ollama", "version": "0.1.25"},
  "capabilities": {"domains": ["chat"], "streaming": true},
  "endpoints": [{"path": "/v1/chat/completions", "method": "POST", "operation_id": "chat.completions.create"}],
  "response_types": {
    "default": "application/json",
    "streaming": "text/event-stream"
  },
  "ui_navigation": {
    "placement": "side-panel",
    "group": "tools"
  },
  "ui": {
    "options": []
  }
}
```

#### Field Specifications

**service.name** (required):
- Must match the service's container/directory name
- Use lowercase with hyphens (e.g., `ollama`, `image-generator`)

**service.version** (recommended):
- Semantic version or date-based (e.g., `1.0.0`, `2026-02-09`)

**endpoints** (required):
- List all operational endpoints exposed by the service
- Each endpoint must have `path`, `method`, and `operation_id`
- `operation_id` should be unique and descriptive (e.g., `chat.completions.create`, `images.generate`)

**capabilities.domains** (recommended):
- Supported domains: `chat`, `image`, `audio`, `video`, `ocr`, `asr`, `tts`, `embedding`, `tool`
- Used by gateway for capability-based routing

**capabilities.modalities** (recommended):
- Input/output modalities: `text`, `image`, `audio`, `video`
- Helps clients understand what the service can process

**capabilities.streaming** (optional):
- `true` if service supports streaming responses (SSE)
- `false` or omitted if only batch responses

**capabilities.max_concurrency** (optional):
- Maximum concurrent requests the service can handle
- Gateway uses this for admission control

**ui.options** (optional):
- UI-renderable configuration options
- Types: `string`, `number`, `integer`, `boolean`, `enum`, `json`
- Used by web UIs to generate forms dynamically

**resources** (optional):
- Recommended resource allocations
- Used by orchestrators for scheduling decisions

#### Example Metadata Response

```json
{
  "schema_version": "v1",
  "service": {
    "name": "ollama",
    "version": "0.1.25",
    "description": "Ollama LLM inference service"
  },
  "backend": {
    "name": "ollama",
    "vendor": "Ollama",
    "base_url": "http://127.0.0.1:11434"
  },
  "endpoints": [
    {
      "path": "/v1/chat/completions",
      "method": "POST",
      "operation_id": "chat.completions.create",
      "summary": "Create a chat completion"
    },
    {
      "path": "/v1/completions",
      "method": "POST",
      "operation_id": "completions.create",
      "summary": "Create a text completion"
    },
    {
      "path": "/v1/models",
      "method": "GET",
      "operation_id": "models.list",
      "summary": "List available models"
    }
  ],
  "capabilities": {
    "domains": ["chat", "embedding"],
    "modalities": ["text"],
    "streaming": true,
    "max_concurrency": 4
  },
  "ui": {
    "options": [
      {
        "key": "model",
        "label": "Model",
        "type": "enum",
        "values": ["llama3.1:8b", "llama3.1:70b", "qwen2.5:14b"],
        "default": "llama3.1:8b",
        "description": "Which model to use for inference"
      },
      {
        "key": "temperature",
        "label": "Temperature",
        "type": "number",
        "default": 0.7,
        "min": 0.0,
        "max": 2.0,
        "description": "Sampling temperature"
      }
    ]
  },
  "resources": {
    "cpu": "4",
    "memory": "8Gi",
    "gpu": "optional"
  }
}
```

## OpenAI-Compatible Endpoints

Services providing AI capabilities should follow OpenAI API conventions where applicable:

### Chat Completions
- **Path**: `/v1/chat/completions`
- **Method**: `POST`
- **Streaming**: Support both streaming (SSE) and non-streaming
- **Request**: OpenAI chat completions format
- **Response**: OpenAI chat completions format

### Text Completions
- **Path**: `/v1/completions`
- **Method**: `POST`
- **Request**: OpenAI completions format
- **Response**: OpenAI completions format

### Embeddings
- **Path**: `/v1/embeddings`
- **Method**: `POST`
- **Request**: OpenAI embeddings format
- **Response**: OpenAI embeddings format

### Images
- **Path**: `/v1/images/generations`
- **Method**: `POST`
- **Request**: OpenAI images format
- **Response**: OpenAI images format (URL or base64)

### Audio
- **Speech (TTS)**: `/v1/audio/speech` (POST)
- **Transcriptions (ASR)**: `/v1/audio/transcriptions` (POST)
- **Translations**: `/v1/audio/translations` (POST)

### Models
- **Path**: `/v1/models`
- **Method**: `GET`
- **Response**: List of available models in OpenAI format

## Service Implementation Checklist

When implementing a new service:

- [ ] Implement `/health` endpoint (liveness)
- [ ] Implement `/readyz` endpoint (readiness) 
- [ ] Implement `/v1/metadata` endpoint with complete metadata
- [ ] Follow OpenAI API conventions for domain-specific endpoints
- [ ] Support streaming where applicable (chat, completions)
- [ ] Include proper error handling and status codes
- [ ] Add structured logging with correlation IDs
- [ ] Expose metrics endpoint (`/metrics`) for Prometheus
- [ ] Document any custom endpoints in metadata
- [ ] Validate service metadata against schema on startup

## Error Handling

All services should use standard HTTP status codes:

- **200 OK**: Successful request
- **400 Bad Request**: Invalid request parameters
- **401 Unauthorized**: Missing or invalid authentication
- **403 Forbidden**: Authenticated but not allowed
- **404 Not Found**: Resource not found
- **422 Unprocessable Entity**: Validation failed
- **429 Too Many Requests**: Rate limit exceeded
- **500 Internal Server Error**: Server error
- **503 Service Unavailable**: Service temporarily unavailable

Error responses should include:
```json
{
  "error": {
    "message": "Human-readable error message",
    "type": "error_type",
    "code": "error_code"
  }
}
```

## Versioning

- API version is in the path (`/v1/...`)
- Breaking changes require a new version (`/v2/...`)
- Metadata schema version tracks separately (`schema_version: "v1"`)
- Services should maintain backward compatibility within a major version

## Testing

Services should include:
- Health endpoint tests (liveness)
- Readiness endpoint tests (with and without backend)
- Metadata endpoint tests (schema validation)
- API endpoint functional tests
- Streaming response tests (if applicable)
- Error handling tests

## Best Practices

1. **Fail Fast**: Return errors quickly rather than hanging
2. **Idempotency**: Support idempotent operations where possible
3. **Timeouts**: Implement reasonable request timeouts
4. **Graceful Shutdown**: Handle SIGTERM and drain connections
5. **Resource Cleanup**: Clean up resources (memory, file handles) properly
6. **Security**: Validate all inputs, sanitize outputs
7. **Observability**: Log structured data with context
8. **Documentation**: Keep metadata endpoint up-to-date

## Examples

See the `services/` directory for reference implementations:
- `services/gateway/`: Full-featured gateway with all patterns
- `services/ollama/`: LLM inference service
- `services/image-gen/`: Image generation service
- `services/tts/`: Text-to-speech service
