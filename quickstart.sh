#!/bin/bash
# Nexus Quick Start Script
# This script helps you get started with Nexus quickly

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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

# Check prerequisites
check_prerequisites() {
    print_header "Checking Prerequisites"
    
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
    
    # Check for GPU support (optional)
    if command -v nvidia-smi &> /dev/null; then
        print_success "NVIDIA GPU detected"
        if docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi &> /dev/null; then
            print_success "NVIDIA Docker runtime configured"
        else
            print_warning "NVIDIA Docker runtime not configured. GPU services will not work."
            print_warning "Install with: sudo apt-get install nvidia-docker2"
        fi
    else
        print_warning "No NVIDIA GPU detected. GPU services will be disabled."
    fi
}

# Setup configuration
setup_config() {
    print_header "Setting Up Configuration"
    
    if [ -f .env ]; then
        print_warning ".env file already exists"
        read -p "Overwrite? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_warning "Keeping existing .env file"
            return
        fi
    fi
    
    cp .env.example .env
    
    # Generate random token
    RANDOM_TOKEN=$(openssl rand -hex 32 2>/dev/null || cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 64 | head -n 1)
    
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
    read -p "Choice (1-4): " -n 1 -r
    echo
    
    case $REPLY in
        1)
            MODEL="llama3.1:8b"
            ;;
        2)
            MODEL="llama3.1:3b"
            ;;
        3)
            MODEL="qwen2.5:14b"
            ;;
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
    
    echo "Which services would you like to start?"
    echo "1) Minimal (Gateway + Ollama)"
    echo "2) Full (All services)"
    read -p "Choice (1-2): " -n 1 -r
    echo
    
    case $REPLY in
        1)
            docker compose up -d
            ;;
        2)
            docker compose --profile full up -d
            ;;
        *)
            print_warning "Invalid choice, starting minimal services"
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
    
    # Check if services are running
    if ! docker compose ps | grep -q "running"; then
        print_error "Services are not running"
        echo "Check logs with: docker compose logs"
        exit 1
    fi
    print_success "Services are running"
    
    # Test gateway health
    if curl -sf http://localhost:8800/health > /dev/null; then
        print_success "Gateway is healthy"
    else
        print_error "Gateway is not responding"
        echo "Check logs with: docker compose logs gateway"
        exit 1
    fi
    
    # Get bearer token from .env
    TOKEN=$(grep GATEWAY_BEARER_TOKEN .env | cut -d '=' -f2)
    
    # Test models endpoint
    if curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8800/v1/models > /dev/null; then
        print_success "API authentication working"
    else
        print_warning "API authentication failed. Check your bearer token."
    fi
}

# Display next steps
show_next_steps() {
    print_header "Setup Complete!"
    
    # Get bearer token
    TOKEN=$(grep GATEWAY_BEARER_TOKEN .env | cut -d '=' -f2)
    
    echo
    echo "Your Nexus instance is ready!"
    echo
    echo "API Endpoint: http://localhost:8800"
    echo "Bearer Token: $TOKEN"
    echo
    echo "Quick test:"
    echo "  curl http://localhost:8800/health"
    echo
    echo "List models:"
    echo "  curl -H \"Authorization: Bearer $TOKEN\" http://localhost:8800/v1/models"
    echo
    echo "Chat completion:"
    cat <<'EOF'
  curl -X POST http://localhost:8800/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "model": "llama3.1:8b",
      "messages": [{"role": "user", "content": "Hello!"}]
    }'
EOF
    echo
    echo "View logs:"
    echo "  docker compose logs -f"
    echo
    echo "Stop services:"
    echo "  docker compose down"
    echo
    echo "Documentation: https://github.com/gevanoff/nexus"
    echo
}

# Main function
main() {
    echo
    echo "╔═══════════════════════════════════════╗"
    echo "║    Nexus Quick Start                 ║"
    echo "║    AI Orchestration Infrastructure   ║"
    echo "╚═══════════════════════════════════════╝"
    echo
    
    check_prerequisites
    setup_config
    start_services
    
    echo
    read -p "Would you like to install a model now? (Y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        # Wait for Ollama to be ready
        sleep 5
        setup_models
    fi
    
    verify_deployment
    show_next_steps
}

# Run main function
main
