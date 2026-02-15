from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/var/lib/gateway/app/.env", extra="ignore")

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    MLX_BASE_URL: str = "http://127.0.0.1:10240/v1"

    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8800
    GATEWAY_BEARER_TOKEN: str

    # Observability listener (local HTTP only)
    OBSERVABILITY_ENABLED: bool = True
    OBSERVABILITY_HOST: str = "127.0.0.1"
    OBSERVABILITY_PORT: int = 8801

    # TLS for incoming connections (gateway server)
    # When both are set, the gateway (uvicorn) can be started with these files
    # to serve HTTPS directly. Typical production deployments use a reverse
    # proxy instead; these settings are provided for convenience in tests
    # or simple single-host deployments.
    GATEWAY_TLS_CERT_PATH: str = ""
    GATEWAY_TLS_KEY_PATH: str = ""

    # Optional multi-token auth (comma-separated). When set, any listed token is accepted.
    # If empty, falls back to single-token GATEWAY_BEARER_TOKEN.
    GATEWAY_BEARER_TOKENS: str = ""

    # Optional per-token policy JSON. Format: {"<token>": { ...policy... }, ...}
    # Policy keys are best-effort and currently used for tool allowlists/rate limits.
    GATEWAY_TOKEN_POLICIES_JSON: str = ""

    # If true and GATEWAY_TOKEN_POLICIES_JSON is set but invalid JSON, fail closed (HTTP 500)
    # rather than silently ignoring policies.
    GATEWAY_TOKEN_POLICIES_STRICT: bool = False

    # Optional request guardrails.
    # - MAX_REQUEST_BYTES: 0 disables. When enabled, requests exceeding this size return 413.
    # - IP_ALLOWLIST: comma-separated IPs and/or CIDRs (e.g. "127.0.0.1,10.0.0.0/8"). Empty allows all.
    MAX_REQUEST_BYTES: int = 1_000_000
    IP_ALLOWLIST: str = ""

    # Optional: restrict tokenless UI endpoints (/ui, /ui/api/*) to specific client IPs/CIDRs.
    # If empty, the UI endpoints are disabled (403) to avoid exposing unauthenticated access.
    UI_IP_ALLOWLIST: str = ""
    # Optional diagnostics for UI allowlist failures.
    # When true, 403 responses include observed client/proxy IP details.
    UI_IP_ALLOWLIST_DEBUG: bool = False
    # Optional: trust proxy headers for UI client IP evaluation, but only when
    # the direct peer IP is in this allowlist (IPs/CIDRs, comma-separated).
    # Example: "172.28.0.1,127.0.0.1"
    UI_TRUST_PROXY_CIDRS: str = ""

    # Optional public base URL for constructing absolute URLs in API responses.
    # When set (e.g. "https://ai2:8800"), image responses that would otherwise return
    # relative paths like "/ui/images/<name>" can instead return fully-qualified URLs.
    # Leave empty to preserve relative URLs.
    PUBLIC_BASE_URL: str = ""

    # Tokenless UI image caching
    # The UI image endpoint can store generated images on disk and return short-lived URLs
    # served by the gateway (still gated by UI_IP_ALLOWLIST).
    UI_IMAGE_DIR: str = "/var/lib/gateway/data/ui_images"
    UI_IMAGE_TTL_SEC: int = 900
    UI_IMAGE_MAX_BYTES: int = 50_000_000

    # Tokenless UI file attachments (chat uploads)
    UI_FILE_DIR: str = "/var/lib/gateway/data/ui_files"
    UI_FILE_TTL_SEC: int = 60 * 60 * 24 * 7  # 7 days
    UI_FILE_MAX_BYTES: int = 100_000_000

    # Tokenless UI chat persistence
    # Stored on disk and served only to allowlisted UI clients (still gated by UI_IP_ALLOWLIST).
    UI_CHAT_DIR: str = "/var/lib/gateway/data/ui_chats"
    UI_CHAT_TTL_SEC: int = 60 * 60 * 24 * 7  # 7 days
    UI_CHAT_MAX_BYTES: int = 2_000_000  # hard cap per conversation file
    UI_CHAT_SUMMARY_TRIGGER_BYTES: int = 250_000  # summarize when history grows beyond this
    UI_CHAT_SUMMARY_KEEP_LAST_MESSAGES: int = 12  # keep tail messages after summarizing

    # User authentication + storage
    USER_AUTH_ENABLED: bool = True
    USER_DB_PATH: str = "/var/lib/gateway/data/users.sqlite"
    USER_SESSION_TTL_SEC: int = 60 * 60 * 12  # 12 hours
    USER_SESSION_COOKIE: str = "gateway_session"

    # Images (text-to-image)
    # Default backend is "mock" which returns an SVG placeholder.
    # Set IMAGES_BACKEND=http_a1111 and IMAGES_HTTP_BASE_URL=http://127.0.0.1:7860 to use Automatic1111's API.
    # Set IMAGES_BACKEND=http_openai_images and IMAGES_HTTP_BASE_URL=http://127.0.0.1:18181 to use an OpenAI-style
    # image server (e.g., Nexa exposing POST /v1/images/generations).
    IMAGES_BACKEND: Literal["mock", "http_a1111", "http_openai_images"] = "mock"
    IMAGES_BACKEND_CLASS: str = "gpu_heavy"  # Backend class for routing/admission control
    IMAGES_HTTP_BASE_URL: str = "http://127.0.0.1:7860"
    IMAGES_HTTP_TIMEOUT_SEC: float = 120.0
    IMAGES_A1111_STEPS: int = 20
    IMAGES_MAX_PIXELS: int = 2_000_000
    IMAGES_OPENAI_MODEL: str = ""
    # Note: Some OpenAI-ish image servers require a model, but others (like the
    # InvokeAI OpenAI-images shim) can use their own configured default if omitted.

    # Request-type routing for images (opt-in): when enabled, model="auto" (or
    # IMAGES_OPENAI_MODEL="auto") selects between FAST/SLOW based on prompt heuristics.
    IMAGES_ENABLE_REQUEST_TYPE: bool = False
    IMAGES_OPENAI_MODEL_FAST: str = "gpu_fast"
    IMAGES_OPENAI_MODEL_SLOW: str = "gpu_slow"

    # HeartMula (music generation)
    # HeartMula is a local HTTP service (see ai-infra/services/heartmula).
    # The gateway uses this base URL when MUSIC_BACKEND=http_heartmula.
    HEARTMULA_BASE_URL: str = ""
    HEARTMULA_TIMEOUT_SEC: float = 120.0
    HEARTMULA_GENERATE_PATH: str = "/v1/music/generations"

    # Music generation routing/admission control.
    # This ties into backends_config.yaml (capability: music).
    MUSIC_BACKEND_CLASS: str = "heartmula_music"

    # Text-to-speech (Pocket TTS)
    # Default path mirrors OpenAI-style POST /v1/audio/speech; override if needed.
    TTS_BASE_URL: str = ""
    TTS_TIMEOUT_SEC: float = 60.0
    TTS_GENERATE_PATH: str = "/v1/audio/speech"
    TTS_BACKEND_CLASS: str = "pocket_tts"
    TTS_CLONE_PATH: str = "/v1/audio/clone"
    LUXTTS_CLONE_PATH: str = "/luxtts/clone"
    QWEN3_TTS_CLONE_PATH: str = "/qwen3-tts/clone"

    # Voice library for cloned voices
    VOICE_LIBRARY_DIR: str = "/var/lib/gateway/data/voice_library"
    VOICE_LIBRARY_MAX_BYTES: int = 50_000_000

    # Optional: SkyReels-V2 video generation shim
    SKYREELS_BASE_URL: str = ""
    SKYREELS_TIMEOUT_SEC: float = 3600.0
    SKYREELS_GENERATE_PATH: str = "/v1/videos/generations"

    # Optional: FollowYourCanvas video generation shim
    FYC_API_BASE_URL: str = ""

    # Optional: LightOnOCR shim
    LIGHTON_OCR_API_BASE_URL: str = ""
    LIGHTON_OCR_TIMEOUT_SEC: float = 120.0

    # Optional: PersonaPlex chat shim (custom UI)
    PERSONAPLEX_BASE_URL: str = ""
    PERSONAPLEX_TIMEOUT_SEC: float = 120.0
    PERSONAPLEX_UI_URL: str = "https://localhost:8998"

    DEFAULT_BACKEND: Literal["ollama", "mlx"] = "ollama"

    # Backends can each have "strong" and "fast" model choices.
    OLLAMA_MODEL_STRONG: str = "qwen2.5:32b"
    OLLAMA_MODEL_FAST: str = "qwen2.5:7b"
    MLX_MODEL_STRONG: str = "mlx-community/gemma-2-2b-it-8bit"
    MLX_MODEL_FAST: str = "mlx-community/gemma-2-2b-it-8bit"

    # Legacy aliases kept for backward compatibility
    OLLAMA_MODEL_DEFAULT: str = "qwen2.5:32b"
    MLX_MODEL_DEFAULT: str = "mlx-community/gemma-2-2b-it-8bit"

    ROUTER_LONG_CONTEXT_CHARS: int = 40_000

    # If true, enable heuristic routing (tools/long-context/fast tier selection).
    # If false (default), routing is strictly alias/prefix/explicit-model driven.
    ROUTER_ENABLE_POLICY: bool = False

    # Request-type routing for chat/completions (opt-in): when enabled alongside
    # ROUTER_ENABLE_POLICY, the router may detect coding requests and prefer the
    # "coder" alias.
    ROUTER_ENABLE_REQUEST_TYPE: bool = False

    # Model alias registry (JSON via env, or JSON file on disk)
    # Example env:
    #   MODEL_ALIASES_JSON='{"aliases":{"coder":{"backend":"ollama","model":"deepseek-coder:33b"}}}'
    MODEL_ALIASES_JSON: str = ""
    MODEL_ALIASES_PATH: str = "/var/lib/gateway/app/model_aliases.json"

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_ALLOW_HTTP_FETCH: bool = False

    TOOLS_ALLOW_GIT: bool = False

    # Safe built-in tools (disabled by default; can be enabled or allowlisted).
    TOOLS_ALLOW_SYSTEM_INFO: bool = False
    TOOLS_ALLOW_MODELS_REFRESH: bool = False

    # Optional explicit allowlist; if set, only these tools may be executed.
    # Example: "read_file,write_file,http_fetch"
    TOOLS_ALLOWLIST: str = ""

    TOOLS_SHELL_CWD: str = "/var/lib/gateway/tools"
    TOOLS_SHELL_TIMEOUT_SEC: int = 20
    TOOLS_SHELL_ALLOWED_CMDS: str = ""  # comma-separated, e.g. "git,rg,ls,cat"

    TOOLS_FS_ROOTS: str = "/var/lib/gateway"  # comma-separated roots
    TOOLS_FS_MAX_BYTES: int = 200_000
    TOOLS_ALLOW_FS_WRITE: bool = False

    TOOLS_HTTP_ALLOWED_HOSTS: str = "127.0.0.1,localhost"
    TOOLS_HTTP_TIMEOUT_SEC: int = 10
    TOOLS_HTTP_MAX_BYTES: int = 200_000

    # Outbound/backend TLS verification
    # - BACKEND_VERIFY_TLS: when false, disable TLS verification for upstreams.
    # - BACKEND_CA_BUNDLE: path to a CA bundle file to use for upstream verification.
    # - BACKEND_CLIENT_CERT: optional client cert for mTLS; either a single
    #   path (PEM containing cert+key) or two paths separated by a comma
    #   ("cert.pem,key.pem").
    BACKEND_VERIFY_TLS: bool = True
    BACKEND_CA_BUNDLE: str = ""
    BACKEND_CLIENT_CERT: str = ""

    # Tool bus JSONL log file path.
    TOOLS_LOG_PATH: str = "/var/lib/gateway/data/tools/invocations.jsonl"

    # Tool invocation logging mode:
    # - ndjson: append-only JSONL at TOOLS_LOG_PATH
    # - per_invocation: one JSON file per replay_id under TOOLS_LOG_DIR
    # - both: do both
    TOOLS_LOG_MODE: Literal["ndjson", "per_invocation", "both"] = "ndjson"
    TOOLS_LOG_DIR: str = "/var/lib/gateway/data/tools"

    # Tool execution hard limits
    TOOLS_MAX_CONCURRENT: int = 8
    TOOLS_CONCURRENCY_TIMEOUT_SEC: float = 5.0
    TOOLS_SUBPROCESS_STDOUT_MAX_CHARS: int = 20000
    TOOLS_SUBPROCESS_STDERR_MAX_CHARS: int = 20000

    # Optional: registry integrity check (sha256 hex). If set and mismatched, registry is ignored.
    TOOLS_REGISTRY_SHA256: str = ""

    # Optional: per-bearer-token rate limit for /v1/tools endpoints.
    # Disabled when <= 0.
    TOOLS_RATE_LIMIT_RPS: float = 0.0
    TOOLS_RATE_LIMIT_BURST: int = 0

    # Optional: metrics endpoint
    METRICS_ENABLED: bool = True

    # Optional infra-owned tool registry (explicit tool declarations).
    # When present, tools can be declared with version + JSON schema + subprocess exec spec.
    TOOLS_REGISTRY_PATH: str = "/var/lib/gateway/app/tools_registry.json"

    TOOLS_GIT_CWD: str = "/var/lib/gateway"
    TOOLS_GIT_TIMEOUT_SEC: int = 20

    EMBEDDINGS_BACKEND: Literal["ollama", "mlx"] = "ollama"
    EMBEDDINGS_MODEL: str = "nomic-embed-text"

    MEMORY_ENABLED: bool = True
    MEMORY_DB_PATH: str = "/var/lib/gateway/data/memory.sqlite"
    MEMORY_TOP_K: int = 6
    MEMORY_MIN_SIM: float = 0.25
    MEMORY_MAX_CHARS: int = 6000

    MEMORY_V2_ENABLED: bool = True
    MEMORY_V2_MAX_AGE_SEC: int = 60 * 60 * 24 * 30
    MEMORY_V2_TYPES_DEFAULT: str = "fact,preference,project"

    # Minimal request instrumentation (JSONL). Intended for debugging/observability.
    REQUEST_LOG_ENABLED: bool = True
    REQUEST_LOG_PATH: str = "/var/lib/gateway/data/requests.jsonl"

    # Agent runtime v1 (single-process, deterministic)
    AGENT_SPECS_PATH: str = "/var/lib/gateway/app/agent_specs.json"
    AGENT_RUNS_LOG_PATH: str = "/var/lib/gateway/data/agent/runs.jsonl"
    AGENT_RUNS_LOG_DIR: str = "/var/lib/gateway/data/agent"
    AGENT_RUNS_LOG_MODE: Literal["ndjson", "per_run", "both"] = "per_run"

    # Admission control / load shedding
    AGENT_BACKEND_CONCURRENCY_OLLAMA: int = 4
    AGENT_BACKEND_CONCURRENCY_MLX: int = 2
    AGENT_QUEUE_MAX: int = 32
    AGENT_QUEUE_TIMEOUT_SEC: float = 2.0
    AGENT_SHED_HEAVY: bool = True


S = Settings()

logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper())
