#!/bin/bash
# Nexus Quick Start Script
# This script helps you get started with Nexus quickly

set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

GATEWAY_HEALTH_URL="http://localhost:8800/health"
GATEWAY_API_URL="http://localhost:8800"

# Helper functions
print_header() {
    echo -e "${GREEN}=== $1 ===${NC}"
}

print_error() {
    echo -e "${RED}ERROR: $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}WARNING: $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

has_optional_dockerfile() {
    local service="$1"
    [[ -f "services/${service}/Dockerfile" ]]
}

# Check prerequisites
check_prerequisites() {
    print_header "Checking Prerequisites"

    if [[ ! -x deploy/scripts/preflight-check.sh ]]; then
        print_warning "Preflight checker missing executable bit; attempting to fix"
        chmod +x deploy/scripts/preflight-check.sh || true
    fi

    if [[ -x deploy/scripts/preflight-check.sh ]]; then
        if ! deploy/scripts/preflight-check.sh; then
            print_error "Preflight checks failed. Resolve failures and retry."
            exit 1
        fi
    fi

    # Check Docker
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed. Please install Docker first."
        echo "Visit: https://docs.docker.com/get-docker/"
        exit 1
    fi
    print_success "Docker found: $(docker --version)"

    # Check Docker Compose
    if ! docker compose version &> /dev/null; then
        print_error "Docker Compose is not installed or not available."
        exit 1
    fi
    print_success "Docker Compose found: $(docker compose version)"

    # Check if Docker daemon is running
    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running. Please start Docker."
        exit 1
    fi
    print_success "Docker daemon is running"
}

# Setup configuration
setup_config() {
    print_header "Setting Up Configuration"

    if [ -f .env ]; then
        print_warning ".env file already exists"
        read -r -p "Overwrite? (y/N) " -n 1 REPLY
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_warning "Keeping existing .env file"
            return
        fi
    fi

    cp .env.example .env
    chmod 600 .env

    # Generate random token
    RANDOM_TOKEN=$(openssl rand -hex 32 2>/dev/null || tr -dc 'a-zA-Z0-9' </dev/urandom | fold -w 64 | head -n 1)

    # Update token in .env
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$RANDOM_TOKEN/" .env
    else
        # Linux
        sed -i "s/GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$RANDOM_TOKEN/" .env
    fi

    print_success "Configuration created"
    print_success "Bearer token: $RANDOM_TOKEN"
    echo "Save this token - you'll need it to access the API!"
    echo
}

# Pull models for Ollama
setup_models() {
    print_header "Setting Up Models"

    echo "Which model would you like to install?"
    echo "1) llama3.1:8b (Recommended - Fast, good quality)"
    echo "2) llama3.1:3b (Faster, lighter)"
    echo "3) qwen2.5:14b (Better quality, slower)"
    echo "4) Skip for now"
    read -r -p "Choice (1-4): " -n 1 REPLY
    echo

    case $REPLY in
        1) MODEL="llama3.1:8b" ;;
        2) MODEL="llama3.1:3b" ;;
        3) MODEL="qwen2.5:14b" ;;
        4)
            print_warning "Skipping model installation"
            return
            ;;
        *)
            print_warning "Invalid choice, skipping model installation"
            return
            ;;
    esac

    print_header "Pulling model: $MODEL"
    echo "This may take a few minutes..."

    if docker compose exec -T ollama ollama pull "$MODEL"; then
        print_success "Model $MODEL installed"
    else
        print_error "Failed to install model $MODEL"
        print_warning "You can install it later with:"
        echo "  docker compose exec ollama ollama pull $MODEL"
    fi
}

# Start services
start_services() {
    print_header "Starting Services"

    local full_available="true"
    if ! has_optional_dockerfile images || ! has_optional_dockerfile tts; then
        full_available="false"
        print_warning "Full profile requires services/images/Dockerfile and services/tts/Dockerfile"
        print_warning "Falling back to minimal startup unless those Dockerfiles are added"
    fi

    echo "Which services would you like to start?"
    echo "1) Minimal (Gateway + Ollama + Etcd)"
    if [[ "$full_available" == "true" ]]; then
        echo "2) Full (All services)"
    else
        echo "2) Full (unavailable in current repo state)"
    fi
    read -r -p "Choice (1-2): " -n 1 REPLY
    echo

    case $REPLY in
        2)
            if [[ "$full_available" == "true" ]]; then
                docker compose --profile full up -d
            else
                docker compose up -d
            fi
            ;;
        *)
            docker compose up -d
            ;;
    esac

    print_success "Services starting..."
    echo "Waiting for services to be ready..."
    sleep 10
}

# Verify deployment
verify_deployment() {
    print_header "Verifying Deployment"

    if ! docker compose ps | grep -q "running"; then
        print_error "Services are not running"
        echo "Check logs with: docker compose logs"
        exit 1
    fi
    print_success "Services are running"

    if curl -sf "$GATEWAY_HEALTH_URL" > /dev/null; then
        print_success "Gateway is healthy"
    else
        print_error "Gateway is not responding"
        echo "Check logs with: docker compose logs gateway"
        exit 1
    fi

    TOKEN=$(grep '^GATEWAY_BEARER_TOKEN=' .env | cut -d '=' -f2)

    if curl -sf -H "Authorization: Bearer $TOKEN" "$GATEWAY_API_URL/v1/models" > /dev/null; then
        print_success "API authentication working"
    else
        print_warning "API authentication failed. Check your bearer token."
    fi
}

# Display next steps
show_next_steps() {
    print_header "Setup Complete!"

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
      "model": "llama3.1:8b",
      "messages": [{"role": "user", "content": "Hello!"}]
    }'
EOS
    echo
    echo "View logs:"
    echo "  docker compose logs -f"
    echo
    echo "Stop services:"
    echo "  docker compose down"
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

    check_prerequisites
    setup_config
    start_services

    echo
    read -r -p "Would you like to install a model now? (Y/n) " -n 1 REPLY
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        sleep 5
        setup_models
    fi

    verify_deployment
    show_next_steps
}

main
