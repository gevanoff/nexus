#!/bin/sh
set -eu

truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|y|on) return 0 ;;
        *) return 1 ;;
    esac
}

model="${NEXUS_VLLM_MODEL:-}"
if [ -z "$model" ]; then
    echo "NEXUS_VLLM_MODEL is required" >&2
    exit 64
fi

served_model_name="${NEXUS_VLLM_SERVED_MODEL_NAME:-$model}"

set -- \
    --host "${NEXUS_VLLM_HOST:-0.0.0.0}" \
    --port "${NEXUS_VLLM_PORT:-8000}" \
    --model "$model" \
    --served-model-name "$served_model_name"

if [ -n "${NEXUS_VLLM_TOKENIZER:-}" ]; then
    set -- "$@" --tokenizer "$NEXUS_VLLM_TOKENIZER"
fi

if [ -n "${NEXUS_VLLM_TOKENIZER_MODE:-}" ]; then
    set -- "$@" --tokenizer-mode "$NEXUS_VLLM_TOKENIZER_MODE"
fi

if [ -n "${NEXUS_VLLM_GENERATION_CONFIG:-vllm}" ]; then
    set -- "$@" --generation-config "${NEXUS_VLLM_GENERATION_CONFIG:-vllm}"
fi

if [ -n "${NEXUS_VLLM_CONFIG_FORMAT:-}" ]; then
    set -- "$@" --config-format "$NEXUS_VLLM_CONFIG_FORMAT"
fi

if [ -n "${NEXUS_VLLM_LIMIT_MM_PER_PROMPT:-}" ]; then
    set -- "$@" --limit-mm-per-prompt "$NEXUS_VLLM_LIMIT_MM_PER_PROMPT"
fi

if truthy "${NEXUS_VLLM_SKIP_MM_PROFILING:-false}"; then
    set -- "$@" --skip-mm-profiling
fi

if [ -n "${NEXUS_VLLM_LOAD_FORMAT:-}" ]; then
    set -- "$@" --load-format "$NEXUS_VLLM_LOAD_FORMAT"
fi

if [ -n "${NEXUS_VLLM_TENSOR_PARALLEL_SIZE:-}" ]; then
    set -- "$@" --tensor-parallel-size "$NEXUS_VLLM_TENSOR_PARALLEL_SIZE"
fi

if [ -n "${NEXUS_VLLM_GPU_MEMORY_UTILIZATION:-}" ]; then
    set -- "$@" --gpu-memory-utilization "$NEXUS_VLLM_GPU_MEMORY_UTILIZATION"
fi

if [ -n "${NEXUS_VLLM_MAX_MODEL_LEN:-}" ]; then
    set -- "$@" --max-model-len "$NEXUS_VLLM_MAX_MODEL_LEN"
fi

if [ -n "${NEXUS_VLLM_CPU_OFFLOAD_GB:-}" ]; then
    set -- "$@" --cpu-offload-gb "$NEXUS_VLLM_CPU_OFFLOAD_GB"
fi

if [ -n "${NEXUS_VLLM_DTYPE:-}" ]; then
    set -- "$@" --dtype "$NEXUS_VLLM_DTYPE"
fi

if [ -n "${NEXUS_VLLM_KV_CACHE_DTYPE:-}" ]; then
    set -- "$@" --kv-cache-dtype "$NEXUS_VLLM_KV_CACHE_DTYPE"
fi

if truthy "${NEXUS_VLLM_CALCULATE_KV_SCALES:-false}"; then
    set -- "$@" --calculate-kv-scales
fi

if [ -n "${NEXUS_VLLM_MAX_NUM_SEQS:-}" ]; then
    set -- "$@" --max-num-seqs "$NEXUS_VLLM_MAX_NUM_SEQS"
fi

if [ -n "${NEXUS_VLLM_MAX_NUM_BATCHED_TOKENS:-}" ]; then
    set -- "$@" --max-num-batched-tokens "$NEXUS_VLLM_MAX_NUM_BATCHED_TOKENS"
fi

if truthy "${NEXUS_VLLM_ENFORCE_EAGER:-false}"; then
    set -- "$@" --enforce-eager
fi

if [ -n "${NEXUS_VLLM_CHAT_TEMPLATE:-}" ]; then
    set -- "$@" --chat-template "$NEXUS_VLLM_CHAT_TEMPLATE"
fi

if [ -n "${NEXUS_VLLM_CHAT_TEMPLATE_CONTENT_FORMAT:-}" ]; then
    set -- "$@" --chat-template-content-format "$NEXUS_VLLM_CHAT_TEMPLATE_CONTENT_FORMAT"
fi

if [ -n "${NEXUS_VLLM_REASONING_PARSER:-}" ]; then
    set -- "$@" --reasoning-parser "$NEXUS_VLLM_REASONING_PARSER"
fi

if truthy "${NEXUS_VLLM_ENABLE_AUTO_TOOL_CHOICE:-false}"; then
    if [ -z "${NEXUS_VLLM_TOOL_CALL_PARSER:-}" ] && [ -z "${NEXUS_VLLM_TOOL_PARSER_PLUGIN:-}" ]; then
        echo "NEXUS_VLLM_ENABLE_AUTO_TOOL_CHOICE requires NEXUS_VLLM_TOOL_CALL_PARSER or NEXUS_VLLM_TOOL_PARSER_PLUGIN" >&2
        exit 64
    fi
    set -- "$@" --enable-auto-tool-choice
    if [ -n "${NEXUS_VLLM_TOOL_CALL_PARSER:-}" ]; then
        set -- "$@" --tool-call-parser "$NEXUS_VLLM_TOOL_CALL_PARSER"
    fi
    if [ -n "${NEXUS_VLLM_TOOL_PARSER_PLUGIN:-}" ]; then
        set -- "$@" --tool-parser-plugin "$NEXUS_VLLM_TOOL_PARSER_PLUGIN"
    fi
    if truthy "${NEXUS_VLLM_EXCLUDE_TOOLS_WHEN_TOOL_CHOICE_NONE:-true}"; then
        set -- "$@" --exclude-tools-when-tool-choice-none
    fi
fi

exec python3 -m vllm.entrypoints.openai.api_server "$@"
