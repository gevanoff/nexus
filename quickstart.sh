#!/bin/bash
# Nexus Quick Start Script
# This script helps you get started with Nexus quickly

# Maintainer note:
# This script intentionally reuses shared helpers from deploy/scripts/_common.sh.
# If you need to change OS detection, dependency installation, prompting, env-file
# behavior, or token generation, prefer updating _common.sh (or adding a helper
# there) rather than duplicating logic here.

set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

GATEWAY_HEALTH_URL="http://localhost:8800/health"
GATEWAY_API_URL="http://localhost:8800"

NS_AUTO_YES="false"

# Compose file layering (policy: one compose file per component).
# Policy reference: see COMPOSE_POLICY.md.
# We pass explicit -f flags so that all compose operations (up/ps/logs/exec)
# consistently target the same stack.
COMPOSE_ARGS=()

usage() {
    cat <<'EOF'
Usage: ./quickstart.sh [--yes]

Options:
  --yes    Non-interactive mode (assume "yes" for install prompts)
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --yes)
                NS_AUTO_YES="true"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                ns_print_error "Unknown argument: $1"
                usage
                exit 2
                ;;
        esac
    done
}
ensure_prerequisites() {
    ns_print_header "Ensuring Prerequisites"
    # docker + curl are required for quickstart; openssl is optional.
    ns_ensure_prereqs true true false false false false || true
}

has_optional_dockerfile() {
    local service="$1"
    [[ -f "services/${service}/Dockerfile" ]]
}

ensure_runtime_layout() {
    ns_print_header "Preparing Runtime Directories"
    ns_ensure_runtime_dirs "$ROOT_DIR"
    ns_seed_gateway_config_files "$ROOT_DIR"
    ns_print_ok "Runtime directories ready under: $ROOT_DIR/.runtime"
}
# Check prerequisites
check_prerequisites() {
    ns_print_header "Checking Prerequisites"

    if [[ ! -x deploy/scripts/preflight-check.sh ]]; then
        ns_print_warn "Preflight checker missing executable bit; attempting to fix"
        chmod +x deploy/scripts/preflight-check.sh || true
    fi

    # Check Docker
    if ! command -v docker &> /dev/null; then
        ns_print_error "Docker is not installed (or not on PATH)."
        exit 1
    fi
    ns_print_ok "Docker found: $(docker --version)"

    # Check Docker Compose
    if ! ns_compose_available; then
        ns_print_error "Docker Compose is not installed or not available (need either 'docker compose' or 'docker-compose')."
        exit 1
    fi
    ns_print_ok "Docker Compose found: $(ns_compose_version)"

    # Check if Docker daemon is running (with best-effort auto-start)
    if ! ns_ensure_docker_daemon true; then
        ns_print_error "Docker daemon is not running. Please start Docker and retry."
        exit 1
    fi
    ns_print_ok "Docker daemon is running"

    if [[ -x deploy/scripts/preflight-check.sh ]]; then
        if ! deploy/scripts/preflight-check.sh --mode quickstart; then
            ns_print_error "Preflight checks failed. Resolve failures and retry."
            exit 1
        fi
    fi
}

# Setup configuration
setup_config() {
    ns_print_header "Setting Up Configuration"

    if [ ! -f .env.example ]; then
        ns_print_error "Missing .env.example. Expected at: $ROOT_DIR/.env.example"
        ns_print_error "If you cloned a partial repo, re-clone the full Nexus repository."
        exit 1
    fi

    if [ -f .env ]; then
        ns_print_warn ".env file already exists"
        if [[ "$NS_AUTO_YES" == "true" ]]; then
            ns_print_warn "Non-interactive mode: keeping existing .env"
            return
        fi
        if ! ns_confirm "Overwrite existing .env?"; then
            ns_print_warn "Keeping existing .env file"
            return
        fi
    fi

    cp .env.example .env
    chmod 600 .env

    # Generate random token (shared helper)
    RANDOM_TOKEN="$(ns_generate_token | tr -d '\r\n')"
    LOCAL_QUICKSTART_MODEL_ALIASES_JSON='{"aliases":{"default":{"backend":"local_vllm","model":"Qwen/Qwen2.5-7B-Instruct","tools":true},"fast":{"backend":"local_vllm_fast","model":"Qwen/Qwen2.5-3B-Instruct","tools":false},"coder":{"backend":"local_vllm","model":"Qwen/Qwen2.5-7B-Instruct","tools":true},"long":{"backend":"local_vllm","model":"Qwen/Qwen2.5-7B-Instruct","context_window":65536,"tools":false},"embeddings":{"backend":"local_vllm_embeddings","model":"BAAI/bge-small-en-v1.5","tools":false}}}'
    platform="$(ns_detect_platform)"

    upsert_env_value() {
        local key="$1"
        local value="$2"
        if grep -q "^${key}=" .env; then
            if [[ "$platform" == "macos" ]]; then
                sed -i '' "s#^${key}=.*#${key}=${value}#" .env
            else
                sed -i "s#^${key}=.*#${key}=${value}#" .env
            fi
        else
            printf '%s=%s\n' "$key" "$value" >> .env
        fi
    }

    # Quickstart is intentionally self-contained: keep local dev on the bundled
    # vLLM services even though the repo's production topology is MLX-first.
    upsert_env_value "GATEWAY_BEARER_TOKEN" "$RANDOM_TOKEN"
    upsert_env_value "VLLM_BASE_URL" "http://vllm:8000/v1"
    upsert_env_value "VLLM_ADVERTISE_BASE_URL" "http://vllm:8000/v1"
    upsert_env_value "VLLM_FAST_BASE_URL" "http://vllm-fast:8000/v1"
    upsert_env_value "VLLM_FAST_ADVERTISE_BASE_URL" "http://vllm-fast:8000/v1"
    upsert_env_value "VLLM_EMBEDDINGS_BASE_URL" "http://vllm-embeddings:8000/v1"
    upsert_env_value "VLLM_EMBEDDINGS_ADVERTISE_BASE_URL" "http://vllm-embeddings:8000/v1"
    upsert_env_value "DEFAULT_BACKEND" "local_vllm"
    upsert_env_value "EMBEDDINGS_BACKEND" "local_vllm_embeddings"
    upsert_env_value "MODEL_ALIASES_JSON" "$LOCAL_QUICKSTART_MODEL_ALIASES_JSON"

    ns_print_ok "Configuration created"
    ns_print_ok "Bearer token: $RANDOM_TOKEN"
    echo "Save this token - you'll need it to access the API!"
    echo
}

# Warm configured vLLM models
setup_models() {
    ns_print_header "Setting Up Models"

    echo "Which vLLM warmup would you like to run?"
    echo "1) Warm strong, fast, and embeddings endpoints"
    echo "2) Check endpoint/model availability only (Recommended)"
    echo "3) Skip for now"
    REPLY="$(ns_read_choice_char "Choice (1-3): " "2" '^[1-3]$')"

    case $REPLY in
        1)
            if bash deploy/scripts/prewarm-vllm.sh; then
                ns_print_ok "vLLM warmup complete"
            else
                ns_print_error "vLLM warmup failed"
            fi
            ;;
        2)
            if bash deploy/scripts/prewarm-vllm.sh --check-only; then
                ns_print_ok "vLLM availability check complete"
            else
                ns_print_error "vLLM availability check failed"
            fi
            ;;
        3)
            ns_print_warn "Skipping vLLM warmup"
            ;;
        *)
            ns_print_warn "Invalid choice, skipping vLLM warmup"
            ;;
    esac
}

# Start services
start_services() {
    ns_print_header "Starting Services"

    # Minimal stack: gateway + vllm + etcd
    COMPOSE_ARGS=(
        -f docker-compose.gateway.yml
        -f docker-compose.vllm.yml
        -f docker-compose.etcd.yml
    )

    local full_available="true"
    if ! has_optional_dockerfile images || ! has_optional_dockerfile tts; then
        full_available="false"
        ns_print_warn "Full profile requires services/images/Dockerfile and services/tts/Dockerfile"
        ns_print_warn "Falling back to minimal startup unless those Dockerfiles are added"
    fi

    echo "Which services would you like to start?"
    echo "1) Core (Gateway + vLLM + Etcd)"
    if [[ "$full_available" == "true" ]]; then
        echo "2) Full (Core + Images + TTS)"
    else
        echo "2) Full (unavailable in current repo state)"
    fi
    REPLY="$(ns_read_choice_char "Choice (1-2): " "1" '^[1-2]$')"

    case $REPLY in
        2)
            if [[ "$full_available" == "true" ]]; then
                ns_compose "${COMPOSE_ARGS[@]}" -f docker-compose.images.yml -f docker-compose.tts.yml up -d
                # Persist full selection for subsequent commands (models/verify/logs)
                COMPOSE_ARGS+=( -f docker-compose.images.yml -f docker-compose.tts.yml )
            else
                ns_compose "${COMPOSE_ARGS[@]}" up -d
            fi
            ;;
        *)
            ns_compose "${COMPOSE_ARGS[@]}" up -d
            ;;
    esac

    ns_print_ok "Services starting..."
    echo "Waiting for services to be ready..."
    sleep 10
}

# Verify deployment
verify_deployment() {
    ns_print_header "Verifying Deployment"

    if ! ns_compose "${COMPOSE_ARGS[@]}" ps | grep -q "running"; then
        ns_print_error "Services are not running"
        echo "Check logs with: $(ns_compose_cmd_string) ${COMPOSE_ARGS[*]} logs"
        exit 1
    fi
    ns_print_ok "Services are running"

    if curl -sf "$GATEWAY_HEALTH_URL" > /dev/null; then
        ns_print_ok "Gateway is healthy"
    else
        ns_print_error "Gateway is not responding"
        echo "Check logs with: $(ns_compose_cmd_string) ${COMPOSE_ARGS[*]} logs gateway"
        exit 1
    fi

    TOKEN=$(grep '^GATEWAY_BEARER_TOKEN=' .env | cut -d '=' -f2)

    if curl -sf -H "Authorization: Bearer $TOKEN" "$GATEWAY_API_URL/v1/models" > /dev/null; then
        ns_print_ok "API authentication working"
    else
        ns_print_warn "API authentication failed. Check your bearer token."
    fi
}

# Display next steps
show_next_steps() {
    ns_print_header "Setup Complete!"

    TOKEN=$(grep '^GATEWAY_BEARER_TOKEN=' .env | cut -d '=' -f2)

    echo
    echo "Your Nexus instance is ready!"
    echo
    echo "API Endpoint: $GATEWAY_API_URL"
    echo "Bearer Token: $TOKEN"
    echo
    echo "Quick test:"
    echo "  curl $GATEWAY_HEALTH_URL"
    echo
    echo "List models:"
    echo "  curl -H \"Authorization: Bearer $TOKEN\" $GATEWAY_API_URL/v1/models"
    echo
    echo "List service registry:"
    echo "  curl -H \"Authorization: Bearer $TOKEN\" $GATEWAY_API_URL/v1/registry"
    echo
    echo "Chat completion:"
    cat <<'EOS'
  curl -X POST http://localhost:8800/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "model": "default",
      "messages": [{"role": "user", "content": "Hello!"}]
    }'
EOS
    echo
    echo "View logs:"
    echo "  $(ns_compose_cmd_string) ${COMPOSE_ARGS[*]} logs -f"
    echo
    echo "Stop services:"
    echo "  $(ns_compose_cmd_string) ${COMPOSE_ARGS[*]} down"
    echo
    echo "Documentation: ./docs"
    echo
}

# Main function
main() {
    echo
    echo "╔═══════════════════════════════════════╗"
    echo "║    Nexus Quick Start                  ║"
    echo "║    AI Orchestration Infrastructure    ║"
    echo "╚═══════════════════════════════════════╝"
    echo

    parse_args "$@"
    ensure_prerequisites
    check_prerequisites
    setup_config
    ensure_runtime_layout
    start_services

    echo
    if ns_confirm_default_yes "Would you like to warm vLLM models now?"; then
        sleep 5
        setup_models
    else
        ns_print_warn "Skipping vLLM warmup"
    fi

    verify_deployment
    show_next_steps
}

main "$@"
