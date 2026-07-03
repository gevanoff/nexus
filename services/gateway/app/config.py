from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/var/lib/gateway/app/.env", extra="ignore")

    VLLM_BASE_URL: str = "http://127.0.0.1:8000/v1"
    VLLM_FAST_BASE_URL: str = "http://127.0.0.1:8001/v1"
    VLLM_EMBEDDINGS_BASE_URL: str = "http://127.0.0.1:8002/v1"
    MLX_BASE_URL: str = "http://127.0.0.1:10240/v1"

    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8800
    GATEWAY_BEARER_TOKEN: str
    TELEGRAM_TOKEN: str = ""
    TELEGRAM_NOTIFY_ENABLED: bool = True
    TELEGRAM_NOTIFY_TIMEOUT_SEC: float = 10.0

    # Observability listener (local HTTP only)
    OBSERVABILITY_ENABLED: bool = True
    OBSERVABILITY_HOST: str = "127.0.0.1"
    OBSERVABILITY_PORT: int = 8801
    HEALTH_CHECK_INTERVAL_SEC: float = 30.0
    HEALTH_CHECK_TIMEOUT_SEC: float = 15.0
    HEALTH_CHECK_FAILURE_THRESHOLD: int = 3
    HEALTH_CHECK_FAILURE_GRACE_SEC: float = 60.0
    MLX_ACTIVE_CANARY_ENABLED: bool = False
    MLX_ACTIVE_CANARY_TIMEOUT_SEC: float = 15.0
    MLX_ACTIVE_CANARY_PROMPT: str = "Reply with the single word OK."
    MLX_ACTIVE_CANARY_MAX_TOKENS: int = 4

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
    # - TRUST_PROXY_CIDRS: when set, if direct peer is trusted, API IP allowlists use
    #   forwarded headers (X-Forwarded-For, X-Real-IP, Forwarded) to evaluate client IP.
    # - IP_ALLOWLIST_DEBUG: when true, 403 responses include resolved peer/header details.
    MAX_REQUEST_BYTES: int = 1_000_000
    IP_ALLOWLIST: str = ""
    TRUST_PROXY_CIDRS: str = ""
    IP_ALLOWLIST_DEBUG: bool = False

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

    # UI model list endpoint tuning
    # - UI_MODELS_PROBE_TIMEOUT_SEC bounds per-backend probe latency when loading models.
    # - UI_MODELS_CACHE_TTL_SEC caches model lists briefly to avoid repeated upstream calls.
    UI_MODELS_PROBE_TIMEOUT_SEC: float = 4.0
    UI_MODELS_CACHE_TTL_SEC: float = 8.0
    MODEL_BENCHMARK_LOG_PATH: str = "/var/lib/gateway/data/model_benchmarks/results.jsonl"

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
    # Chat context shaping for the UI chat endpoint.
    # Default is conversational mode: prior messages are folded into system context so
    # the browser chat behaves like a normal ongoing conversation.
    # Set false to force strict single-message behavior.
    UI_CHAT_INCLUDE_PRIOR_CONTEXT: bool = True

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
    # IMAGES_BACKEND=local_mlx is also accepted as an alias for the direct MLX/OpenAI-compatible image path.
    IMAGES_BACKEND: Literal["mock", "http_a1111", "http_openai_images", "local_mlx", "mlx"] = "mock"
    IMAGES_BACKEND_CLASS: str = "gpu_heavy"  # Backend class for routing/admission control
    IMAGES_HTTP_BASE_URL: str = "http://images:7860"
    IMAGES_HTTP_TIMEOUT_SEC: float = 120.0
    SDXL_TURBO_BASE_URL: str = ""
    INVOKEAI_BASE_URL: str = ""
    INVOKEAI_ADVERTISE_BASE_URL: str = ""
    INVOKEAI_UI_URL: str = ""
    IMAGES_A1111_STEPS: int = 20
    IMAGES_MAX_PIXELS: int = 2_000_000
    IMAGES_OPENAI_MODEL: str = ""
    # Note: Some OpenAI-ish image servers require a model, but others (like the
    # InvokeAI OpenAI-images shim) can use their own configured default if omitted.

    # Request-type routing for images (opt-in): when enabled, model="auto" (or
    # IMAGES_OPENAI_MODEL="auto") selects between FAST/SLOW based on prompt heuristics.
    IMAGES_ENABLE_REQUEST_TYPE: bool = False
    IMAGES_OPENAI_MODEL_FAST: str = "gpu_fast"
    IMAGES_OPENAI_MODEL_SLOW: str = "gpu_heavy"

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
    POCKET_TTS_BASE_URL: str = "http://tts:9940"
    LUXTTS_BASE_URL: str = "http://luxtts:9170"
    QWEN3_TTS_BASE_URL: str = "http://qwen3-tts:9175"
    TTS_TIMEOUT_SEC: float = 300.0
    TTS_GENERATE_PATH: str = "/v1/audio/speech"
    TTS_BACKEND_CLASS: str = "pocket_tts"
    TTS_CLONE_PATH: str = "/v1/audio/clone"
    LUXTTS_CLONE_PATH: str = "/luxtts/clone"
    QWEN3_TTS_CLONE_PATH: str = "/qwen3-tts/clone"

    # Audio transcription (Whisper/OpenAI-compatible)
    TRANSCRIPTION_BACKEND_CLASS: str = "local_mlx"
    TRANSCRIPTION_TIMEOUT_SEC: float = 600.0
    TRANSCRIPTION_MODEL: str = ""

    # Voice library for cloned voices
    VOICE_LIBRARY_DIR: str = "/var/lib/gateway/data/voice_library"
    VOICE_LIBRARY_MAX_BYTES: int = 50_000_000

    # Optional: SkyReels-V2 video generation shim
    SKYREELS_V2_BASE_URL: str = ""
    SKYREELS_BASE_URL: str = ""
    SKYREELS_TIMEOUT_SEC: float = 3600.0
    SKYREELS_GENERATE_PATH: str = "/v1/videos/generations"
    VIDEO_BACKEND_CLASS: str = "skyreels_v2"

    # Optional: FollowYourCanvas video generation shim
    FOLLOWYOURCANVAS_BASE_URL: str = ""
    FYC_API_BASE_URL: str = ""
    FYC_TIMEOUT_SEC: float = 1800.0
    FYC_GENERATE_PATH: str = "/v1/videos/generations"

    # Optional: LightOnOCR shim
    LIGHTON_OCR_API_BASE_URL: str = ""
    LIGHTON_OCR_TIMEOUT_SEC: float = 120.0
    OCR_BACKEND_CLASS: str = "lighton_ocr"

    # Optional: PersonaPlex chat shim (custom UI)
    PERSONAPLEX_BASE_URL: str = ""
    PERSONAPLEX_ADVERTISE_BASE_URL: str = ""
    PERSONAPLEX_TIMEOUT_SEC: float = 120.0
    PERSONAPLEX_UI_URL: str = ""
    PERSONAPLEX_UI_SCHEME: str = "https"
    PERSONAPLEX_UI_PORT: int = 8998

    # Optional backend lifecycle/resource manager.
    LIFECYCLE_MANAGER_BASE_URL: str = ""
    LIFECYCLE_MANAGER_TIMEOUT_SEC: float = 15.0
    NEXUS_HARDWARE_REFRESH_ON_STARTUP: bool = True
    NEXUS_HARDWARE_REFRESH_TIMEOUT_SEC: float = 45.0
    NEXUS_HARDWARE_REFRESH_ATTEMPTS: int = 3
    NEXUS_HARDWARE_REFRESH_RETRY_DELAY_SEC: float = 5.0
    NEXUS_HARDWARE_SNAPSHOT_PATH: str = "/var/lib/gateway/data/nexus_hardware_snapshot.json"

    # Optional comma-separated backend classes to omit from the active registry.
    # Useful when a backend exists in static config but is intentionally not
    # provisioned on the current cluster.
    DISABLED_BACKEND_CLASSES: str = ""

    DEFAULT_BACKEND: str = "local_mlx"

    # Service discovery (etcd)
    ETCD_ENABLED: bool = True
    ETCD_URL: str = "http://etcd:2379"
    ETCD_PREFIX: str = "/nexus/services/"
    ETCD_POLL_INTERVAL: float = 15.0
    ETCD_SEED_FROM_ENV: bool = True
    BACKEND_ENV_BASE_URL_OVERRIDES: str = ""
    ETCD_TIMEOUT_SEC: float = 5.0

    # vLLM-backed lightweight/utility models, typically hosted on Linux/NVIDIA
    # nodes such as ai1 or ada2.
    VLLM_MODEL_STRONG: str = "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic"
    VLLM_MODEL_FAST: str = "cyankiwi/Devstral-Small-2507-AWQ-4bit"
    VLLM_MODEL_DEFAULT: str = "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic"
    VLLM_MODEL_EMBEDDINGS: str = "BAAI/bge-small-en-v1.5"
    VLLM_MAX_MODEL_LEN: int = 8192
    VLLM_FAST_MAX_MODEL_LEN: int = 8192
    VLLM_NATIVE_TOOLS_ENABLED: bool = False
    VLLM_FAST_NATIVE_TOOLS_ENABLED: bool = False

    # MLX-hosted reasoning models, typically on ai2.
    MLX_MODEL_STRONG: str = "mlx-community/GLM-5.2-DQ4plus-q8"
    MLX_MODEL_FAST: str = "mlx-community/Phi-4-reasoning-plus-4bit"
    MLX_MODEL_DEFAULT: str = "mlx-community/GLM-5.2-DQ4plus-q8"
    MLX_FALLBACK_BACKEND: str = "local_mlx"
    MLX_FALLBACK_MODEL: str = "mlx-community/MiniMax-M3-4bit"
    MLX_HF_CACHE_DIR: str = "/var/lib/gateway/mlx_hf_cache"
    MLX_FETCH_STALLED_AFTER_SEC: int = 600
    MLX_HUGE_LANE_ENABLED: bool = True
    MLX_HUGE_LANE_DEFAULT_MODEL: str = "mlx-community/GLM-5.2-DQ4plus-q8"
    MLX_HUGE_MODELS: str = "mlx-community/GLM-5.2-DQ4plus-q8,mlx-community/MiniMax-M3-4bit,mlx-community/DeepSeek-R1-0528-4bit"
    MLX_HUGE_LANE_STATE_PATH: str = "/var/lib/gateway/data/mlx_huge_lane.json"
    # GLM-5.2 prefill is serialized inside its MLX handler process. Bound the
    # serialized input so one oversized conversation cannot monopolize it past
    # mlx-openai-server's 300-second RPC timeout.
    MLX_GLM_MAX_INPUT_CHARS: int = 60_000

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
    #   MODEL_ALIASES_JSON='{"aliases":{"coder":{"backend":"local_mlx","model":"mlx-community/GLM-5.2-DQ4plus-q8"}}}'
    MODEL_ALIASES_JSON: str = ""
    MODEL_ALIASES_PATH: str = "/var/lib/gateway/config/model_aliases.json"

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_ALLOW_HTTP_FETCH: bool = False

    TOOLS_ALLOW_GIT: bool = False

    # Safe built-in tools (disabled by default; can be enabled or allowlisted).
    TOOLS_ALLOW_SYSTEM_INFO: bool = False
    TOOLS_ALLOW_MODELS_REFRESH: bool = False
    TOOLS_ALLOW_CLUSTER_RESOURCES: bool = True

    # Optional explicit allowlist; if set, only these tools may be executed.
    # Example: "read_file,write_file,http_fetch"
    TOOLS_ALLOWLIST: str = ""
    TOOLS_ALLOW_WEB_BROWSE: bool = True

    TOOLS_SHELL_CWD: str = "/var/lib/gateway/tools"
    TOOLS_SHELL_TIMEOUT_SEC: int = 20
    TOOLS_SHELL_ALLOWED_CMDS: str = ""  # comma-separated, e.g. "git,rg,ls,cat"

    TOOLS_FS_ROOTS: str = "/var/lib/gateway/app,/var/lib/gateway/tools,/var/lib/gateway/data/tools_work"  # comma-separated roots
    TOOLS_FS_MAX_BYTES: int = 200_000
    TOOLS_ALLOW_FS_WRITE: bool = False

    TOOLS_HTTP_ALLOWED_HOSTS: str = "127.0.0.1,localhost"
    TOOLS_HTTP_TIMEOUT_SEC: int = 10
    TOOLS_HTTP_MAX_BYTES: int = 200_000
    TOOLS_WEB_BROWSE_TIMEOUT_SEC: float = 20.0
    TOOLS_WEB_BROWSE_MAX_BYTES: int = 1_000_000
    TOOLS_WEB_BROWSE_ALLOWED_HOSTS: str = "*"
    TOOLS_WEB_BROWSE_ALLOW_PRIVATE: bool = False
    TOOLS_WEB_BROWSE_MAX_REDIRECTS: int = 4

    # Outbound/backend TLS verification
    # - BACKEND_VERIFY_TLS: when false, disable TLS verification for upstreams.
    # - BACKEND_CA_BUNDLE: path to a CA bundle file to use for upstream verification.
    # - BACKEND_CLIENT_CERT: optional client cert for mTLS; either a single
    #   path (PEM containing cert+key) or two paths separated by a comma
    #   ("cert.pem,key.pem").
    BACKEND_VERIFY_TLS: bool = True
    BACKEND_CA_BUNDLE: str = ""
    BACKEND_CLIENT_CERT: str = ""
    # Retry connection establishment only. httpx/httpcore does not retry a
    # request after bytes have been sent, so POST bodies are not duplicated.
    BACKEND_CONNECT_RETRIES: int = 2

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
    TOOLS_REGISTRY_PATH: str = "/var/lib/gateway/config/tools_registry.json"

    TOOLS_GIT_CWD: str = "/var/lib/gateway"
    TOOLS_GIT_TIMEOUT_SEC: int = 20

    EMBEDDINGS_BACKEND: str = "local_mlx"
    EMBEDDINGS_MODEL: str = ""

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
    OPENAI_DEBUG_LOG_MESSAGE_CONTENT: bool = False
    GATEWAY_DEBUG_OPENAI_REQUESTS: bool = False

    # Agent runtime v1 (single-process, deterministic)
    AGENT_SPECS_PATH: str = "/var/lib/gateway/config/agent_specs.json"
    AGENT_TASK_SPECS_PATH: str = "/var/lib/gateway/data/agent/agent_specs.json"
    AGENT_RUNS_LOG_PATH: str = "/var/lib/gateway/data/agent/runs.jsonl"
    AGENT_RUNS_LOG_DIR: str = "/var/lib/gateway/data/agent"
    AGENT_RUNS_LOG_MODE: Literal["ndjson", "per_run", "both"] = "per_run"

    # Durable agent scheduled tasks.
    # Agents can create countdown-style one-shot tasks or recurring interval/cron
    # checks. The scheduler stores state in SQLite and executes due tasks through
    # AgentRuntimeV1, so normal agent specs, tool tiers, IO budgets, and run logs apply.
    AGENT_TASKS_ENABLED: bool = True
    AGENT_TASKS_DB_PATH: str = "/var/lib/gateway/data/agent/tasks.sqlite"
    AGENT_TASKS_POLL_INTERVAL_SEC: float = 5.0
    AGENT_TASKS_MAX_DUE_PER_TICK: int = 3
    AGENT_TASKS_MIN_DELAY_SEC: int = 5
    AGENT_TASKS_RUN_TIMEOUT_SEC: float = 1800.0

    # Continuous Nexus Sentinel runtime.
    NEXUS_SENTINEL_ENABLED: bool = True
    NEXUS_SENTINEL_DB_PATH: str = "/var/lib/gateway/data/sentinel/sentinel.sqlite"
    NEXUS_SENTINEL_POLL_INTERVAL_SEC: float = 15.0
    NEXUS_SENTINEL_STALLED_AFTER_SEC: float = 900.0
    NEXUS_SENTINEL_RESOURCE_PRESSURE_PCT: float = 0.9
    NEXUS_SENTINEL_BACKEND_ISSUE_MIN_POLLS: int = 3
    NEXUS_SENTINEL_BACKEND_ISSUE_MIN_SEC: int = 60
    NEXUS_SENTINEL_RESUME_COOLDOWN_SEC: int = 1800
    NEXUS_SENTINEL_NOTIFICATION_COOLDOWN_SEC: int = 6 * 60 * 60
    NEXUS_SENTINEL_MAX_EVENTS: int = 5000
    NEXUS_SENTINEL_ARCHIVE_ANALYSIS_MAX_DIFF_CHARS: int = 12_000

    # Admission control / load shedding
    AGENT_BACKEND_CONCURRENCY_VLLM: int = 4
    AGENT_BACKEND_CONCURRENCY_MLX: int = 2
    AGENT_QUEUE_MAX: int = 32
    AGENT_QUEUE_TIMEOUT_SEC: float = 2.0
    AGENT_SHED_HEAVY: bool = True

    # Multi-backend coordinator
    COORDINATOR_DEFAULT_PARTICIPANTS: str = "default"
    COORDINATOR_DEFAULT_SYNTHESIZER: str = "default"
    COORDINATOR_INCLUDE_VISION_ON_MEDIA: bool = True
    COORDINATOR_INCLUDE_CODER_ON_CODE: bool = True
    COORDINATOR_MAX_PARTICIPANTS: int = 6
    COORDINATOR_PARALLEL_TIMEOUT_SEC: float = 300.0

    # Nexus coding workspaces.
    # These endpoints create isolated git clones under the gateway data dir and
    # expose constrained file, git, shell, push, and PR operations for coding AIs.
    CODING_ENABLED: bool = True
    CODING_ALLOW_BEARER_API: bool = True
    CODING_REQUIRE_ADMIN: bool = True
    CODING_WORKSPACE_ROOT: str = "/var/lib/gateway/data/coding/workspaces"
    CODING_TASKS_DIR: str = "/var/lib/gateway/data/coding/tasks"
    CODING_DEFAULT_REPO_URL: str = "https://github.com/gevanoff/nexus.git"
    CODING_ALLOWED_REPOS: str = "https://github.com/gevanoff/nexus.git"
    CODING_ALLOWED_REPOS_JSON: str = ""
    CODING_DEFAULT_BASE_BRANCH: str = "main"
    CODING_BRANCH_PREFIX: str = "nexus-coder"
    CODING_ALLOWED_COMMANDS: str = "git,rg,python,python3,node,npm,pytest,ruff,uv,gh"
    CODING_COMMAND_TIMEOUT_SEC: int = 120
    CODING_ARCHIVE_RETENTION_SEC: int = 7 * 24 * 60 * 60
    CODING_SMOKE_REPORT_DIR: str = "/var/lib/gateway/coding_smoke_reports"
    CODING_SMOKE_SCHEDULER_ENABLED: bool = False
    CODING_SMOKE_RUN_AT_STARTUP: bool = True
    CODING_SMOKE_START_INTERVAL_SEC: int = 3600
    CODING_SMOKE_MODELS: str = "coder,default,reasoning"
    CODING_SMOKE_PROFILES: str = "fixture_median,fixture_inventory,fixture_route_flags"
    CODING_SMOKE_WEEKLY_MODELS: str = ""
    CODING_SMOKE_WEEKLY_PROFILES: str = "fixture_median,fixture_inventory,fixture_route_flags"
    CODING_SMOKE_WEEKLY_DAY: int = 7
    CODING_SMOKE_IDLE_START_HOUR: int = 0
    CODING_SMOKE_IDLE_END_HOUR: int = 6
    CODING_SMOKE_TIMEOUT_SEC: int = 1200
    CODING_SMOKE_POLL_SEC: int = 10
    CODING_SMOKE_STALLED_AFTER_SEC: int = 180
    CODING_MAX_OUTPUT_CHARS: int = 40_000
    CODING_FILE_MAX_BYTES: int = 500_000
    CODING_AGENT_MAX_EVENTS: int = 120
    CODING_AGENT_MAX_TOOL_RESULT_CHARS: int = 100_000
    CODING_AGENT_MAX_TOKENS: int = 8192
    CODING_AGENT_TEXT_TOOL_MAX_TOKENS: int = 64
    CODING_AGENT_TOOL_CONTEXT_CHARS: int = 32_000
    CODING_AGENT_MAX_NO_TOOL_CYCLES: int = 4
    CODING_AGENT_MAX_SEMANTIC_REROUTES: int = 1
    CODING_AGENT_CHECKPOINT_COMMITS: bool = True
    CODING_AGENT_BACKEND_RETRIES: int = 2
    CODING_AGENT_BACKEND_RETRY_BASE_DELAY_SEC: float = 10.0
    CODING_AGENT_BACKEND_RETRY_MAX_DELAY_SEC: float = 60.0
    CODING_AGENT_BACKEND_RETRY_STATUSES: str = "500,502,503,504"
    CODING_AGENT_QUEUE_TIMEOUT_SEC: float = 30.0
    CODING_AGENT_QUEUE_POLL_SEC: float = 1.0
    CODING_AGENT_MAX_CYCLES_PER_RUN: int = 80
    CODING_AGENT_MAX_RUNTIME_SEC: int = 21_600
    CODING_AGENT_CONTEXT_RESET_CYCLES: int = 12
    CODING_AGENT_CONTEXT_RESET_CHARS: int = 40_000
    CODING_GIT_USERNAME: str = "x-access-token"
    CODING_GIT_TOKEN: str = ""
    CODING_GIT_AUTHOR_NAME: str = "Nexus Coding Agent"
    CODING_GIT_AUTHOR_EMAIL: str = "nexus-coder@localhost"


S = Settings()

logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper())
