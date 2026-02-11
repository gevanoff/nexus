# Migration Guide: ai-infra to Nexus

This guide helps you migrate from the host-based `ai-infra` deployment to the container-based Nexus infrastructure.

## Overview

Nexus replaces the `ai-infra` macOS/Linux host-based deployment with a unified Docker container approach.

Operator environment: **macOS/Linux**. If you develop on Windows, run Nexus scripts from **WSL** and use SSH from WSL when targeting Linux hosts.

### Key Differences

| Aspect | ai-infra | Nexus |
|--------|----------|-------|
| **Deployment** | launchd/systemd scripts | Docker Compose |
| **Platform** | macOS/Linux specific | Any Docker platform (operated on macOS/Linux) |
| **Installation** | Manual host setup | Container images |
| **Networking** | Host ports + SSH | Docker networks |
| **Data** | `/var/lib/*/` | Host bind mounts under `./.runtime/` |
| **Updates** | Manual scripts | `docker compose pull` |
| **Configuration** | Scattered env files | Centralized `.env` |
| **Service Discovery** | Static config | Dynamic via `/v1/metadata` |

## Pre-Migration Checklist

Before migrating:

- [ ] Document current service configuration
- [ ] Backup all data directories
- [ ] Export Ollama model lists
- [ ] Note custom configurations
- [ ] Test backup restoration
- [ ] Plan migration window
- [ ] Notify users of downtime

## Recommended Migration Path (Scripted)

The automated migration script handles backup, deployment, restore, and verification. This is the recommended approach for most users.

### Step 1: Ensure Scripts are Executable

```bash
chmod +x deploy/scripts/*.sh quickstart.sh
```

### Step 2: Install Docker/Docker Compose (If Needed)

Run the host dependency installer if Docker/Docker Compose are not already present (interactive):

```bash
./deploy/scripts/install-host-deps.sh
```

This script prompts before any privileged package/runtime installation and supports optional NVIDIA runtime setup.

### Step 3: Run the Migration Helper

Interactive mode (recommended):

```bash
./deploy/scripts/migrate-from-ai-infra.sh
```

The migration helper prompts for and/or automates:
- ai-infra backup creation (gateway data, Ollama model list, optional full Ollama archive)
- Nexus `.env` initialization and token generation
- `docker compose up -d`
- Restore of gateway/Ollama data and config backups
- Post-migration validation (`docker compose ps`, `/health`, optional `/v1/models`)
- Optional shutdown of legacy ai-infra services

### Non-Interactive Usage

For automated runs, provide all required paths and flags:

```bash
./deploy/scripts/migrate-from-ai-infra.sh \
  --ai-infra-dir "$HOME/ai-infra" \
  --backup-dir "$HOME/nexus-migration-backup" \
  --nexus-dir "$(pwd)" \
  --yes
```

Optional flags:
- `--skip-deploy`: backup + prep without `docker compose up -d`
- `--skip-restore`: skip restore steps and only perform backup/deploy/verify

### What the Script Automates

The migration script handles the following tasks that were previously manual:
- Docker and Docker Compose installation (via `install-host-deps.sh`)
- Optional NVIDIA runtime installation
- Backup/restore command sequences
- Container copy/extract commands for migration artifacts
- Migration verification command sequence
- Optional legacy service shutdown

### Post-Migration Checks

After script completion, verify the migration:

```bash
docker compose ps
docker compose logs --tail=100 gateway ollama

# Test API endpoints
curl http://localhost:8800/health
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8800/v1/models
```

---

## Manual Migration Steps (Reference)

The following manual steps are provided as a reference for users who prefer manual control or need to troubleshoot the automated script. Most users should use the scripted approach above.

### Step 1: Backup Current Setup

#### Backup Gateway Data

```bash
# On host with ai-infra
sudo tar czf ~/gateway-backup.tar.gz -C /var/lib/gateway/data .
```

#### Backup Ollama Models

```bash
# List currently installed models
ollama list > ~/ollama-models.txt

# Backup model files (optional, can re-download)
sudo tar czf ~/ollama-backup.tar.gz -C /var/lib/ollama .
```

#### Backup Configuration Files

```bash
cd ~/ai-infra  # or wherever ai-infra is located

# Backup gateway config
cp services/gateway/env/gateway.env ~/gateway.env.backup

# Backup model aliases
cp services/gateway/env/model_aliases.json.example ~/model_aliases.json.backup

# Backup tool registry
cp services/gateway/env/tools_registry.json.example ~/tools_registry.json.backup
```

### Step 2: Install Docker

If not already installed:

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# macOS
# Install Docker Desktop from docker.com

# Log out and back in for group changes to take effect
```

### Step 3: Install NVIDIA Container Toolkit (GPU Hosts Only)

If you need GPU support:

```bash
# Ubuntu/Debian
distribution=$(. /etc/os-release;echo $ID$VERSION_ID) \
   && curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add - \
   && curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker

# Test
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

### Step 4: Deploy Nexus

Clone Nexus repository:

```bash
git clone https://github.com/gevanoff/nexus.git
cd nexus
```

Configure environment:

```bash
cp .env.example .env
nano .env
```

Map your ai-infra settings to Nexus:

```bash
# ai-infra gateway.env → Nexus .env

# GATEWAY_BEARER_TOKEN (same in both)
GATEWAY_BEARER_TOKEN=your-token-here

# OLLAMA_BASE_URL (changes from host to container name)
# Old: http://127.0.0.1:11434
# New: http://ollama:11434 (set automatically)

# IMAGES_HTTP_BASE_URL (changes from host to container name)
# Old: http://ada2:7860
# New: http://images:7860 (set automatically)
```

Start services:

```bash
docker compose up -d
```

### Step 5: Restore Data

#### Restore Gateway Data

```bash
# Copy backup to container
docker cp ~/gateway-backup.tar.gz nexus-gateway:/tmp/

# Extract in container
docker compose exec gateway tar xzf /tmp/gateway-backup.tar.gz -C /var/lib/gateway/data

# Verify
docker compose exec gateway ls -la /var/lib/gateway/data
```

#### Restore Ollama Models

Option A: Pull models again (recommended):

```bash
# Read model list from backup
while read model; do
  docker compose exec ollama ollama pull "$model"
done < ~/ollama-models.txt
```

Option B: Restore model files:

```bash
# Copy backup
docker cp ~/ollama-backup.tar.gz nexus-ollama:/tmp/

# Extract (warning: may be incompatible across versions)
docker compose exec ollama tar xzf /tmp/ollama-backup.tar.gz -C /root/.ollama
docker compose restart ollama
```

### Step 6: Migrate Custom Configuration

#### Model Aliases

If you had custom model aliases in `model_aliases.json`:

```bash
# Place on host (mounted read-only inside container)
cp ~/model_aliases.json.backup ./.runtime/gateway/config/model_aliases.json
docker compose restart gateway
```

#### Tool Registry

If you had custom tools in `tools_registry.json`:

```bash
# Place on host (mounted read-only inside container)
cp ~/tools_registry.json.backup ./.runtime/gateway/config/tools_registry.json
docker compose restart gateway
```

#### Agent Specs

If you had custom agent specs:

```bash
cp ~/agent_specs.json ./.runtime/gateway/config/agent_specs.json
docker compose restart gateway
```

### Step 7: Verify Migration

Test basic functionality:

```bash
# Health check
curl http://localhost:8800/health

# Models list
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8800/v1/models

# Chat completion
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "test"}]
  }'
```

Check service health:

```bash
docker compose ps
docker compose logs --tail=100 gateway ollama
```

### Step 8: Update Client Applications

Update client apps to point to new URL:

```bash
# Old: https://ai2:8800
# New: http://localhost:8800 (or your Docker host)

# If using remote access, update to Docker host IP/domain
```

### Step 9: Stop Old Services

Once verified, stop ai-infra services:

```bash
cd ~/ai-infra

# Stop gateway
./services/gateway/scripts/uninstall.sh

# Stop Ollama (if running via ai-infra)
./services/ollama/scripts/uninstall.sh

# On macOS, remove launchd plists
sudo rm /Library/LaunchDaemons/com.ai.gateway.plist
sudo rm /Library/LaunchDaemons/com.ollama.service.plist
sudo launchctl bootout system/com.ai.gateway
sudo launchctl bootout system/com.ollama.service
```

## Service-Specific Migration

### Gateway Service

| ai-infra Path | Nexus Location |
|---------------|----------------|
| `/var/lib/gateway/app` | Container `/app` |
| `/var/lib/gateway/data` | Host `./.runtime/gateway/data` → container `/var/lib/gateway/data` |
| (operator config) | Host `./.runtime/gateway/config` → container `/var/lib/gateway/config` (read-only) |
| `/var/lib/gateway/env` | N/A (uses Docker image deps) |
| `/var/log/gateway/` | Docker logs |

```bash
# View logs (old)
tail -f /var/log/gateway/gateway.out.log

# View logs (new)
docker compose logs -f gateway
```

### Ollama Service

| ai-infra Path | Nexus Location |
|---------------|----------------|
| `/var/lib/ollama/models` | Host `./.runtime/ollama` → container `/root/.ollama` |
| `/var/log/ollama/` | Docker logs |

```bash
# Pull model (old)
ollama pull llama3.1:8b

# Pull model (new)
docker compose exec ollama ollama pull llama3.1:8b
```

### Image Generation (InvokeAI)

| ai-infra Host | Nexus Service |
|---------------|---------------|
| `ada2` host | `images` container |
| Port `7860` | Port `7860` |

Gateway config:

```bash
# Old
IMAGES_BACKEND=http_openai_images
IMAGES_HTTP_BASE_URL=http://ada2:7860

# New (automatic)
IMAGES_BACKEND=http_openai_images
IMAGES_HTTP_BASE_URL=http://images:7860
```

## Configuration Mapping

### Environment Variables

| ai-infra | Nexus | Notes |
|----------|-------|-------|
| `GATEWAY_BEARER_TOKEN` | `GATEWAY_BEARER_TOKEN` | Same |
| `OLLAMA_BASE_URL` | Auto-set | Uses Docker service name |
| `MEMORY_DB_PATH` | Auto-set | In bind-mounted `./.runtime/gateway/data` |
| `UI_IMAGE_DIR` | Auto-set | In bind-mounted `./.runtime/gateway/data` |
| `TOOLS_LOG_PATH` | Auto-set | In bind-mounted `./.runtime/gateway/data` |

Note: Nexus uses host bind mounts under `./.runtime/` (not Docker named volumes).

### Port Mappings

| Service | ai-infra | Nexus Host Port | Nexus Container |
|---------|----------|-----------------|-----------------|
| Gateway | `ai2:8800` | `8800` | `gateway:8800` |
| Observability | `ai2:8801` | `8801` | `gateway:8801` |
| Ollama | `ai1:11434` | `11434` | `ollama:11434` |
| InvokeAI | `ada2:7860` | `7860` | `images:7860` |

### Data Directories

```bash
# ai-infra data locations → Nexus bind mounts (repo-local by default)

/var/lib/gateway/data/              → ./.runtime/gateway/data/
/var/lib/gateway/config (operator)  → ./.runtime/gateway/config/
/var/lib/gateway/data/memory.sqlite → ./.runtime/gateway/data/memory.sqlite
/var/lib/gateway/data/users.sqlite  → ./.runtime/gateway/data/users.sqlite
/var/lib/gateway/data/ui_images/    → ./.runtime/gateway/data/ui_images/

/var/lib/ollama/models/             → ./.runtime/ollama/
```

## Rollback Plan

If migration fails, roll back to ai-infra:

```bash
# Stop Nexus
cd ~/nexus
docker compose down

# Restart ai-infra services
cd ~/ai-infra
./services/gateway/scripts/restart.sh
./services/ollama/scripts/restart.sh

# Restore data if needed
sudo rm -rf /var/lib/gateway/data
sudo tar xzf ~/gateway-backup.tar.gz -C /var/lib/gateway/data
sudo chown -R gateway:gateway /var/lib/gateway/data
./services/gateway/scripts/restart.sh
```

## Post-Migration

### Cleanup

Once confident in migration:

```bash
# Remove ai-infra data (optional)
sudo rm -rf /var/lib/gateway
sudo rm -rf /var/lib/ollama
sudo rm -rf /var/log/gateway
sudo rm -rf /var/log/ollama

# Remove ai-infra repository (optional)
rm -rf ~/ai-infra
```

### Monitoring

Set up monitoring for new deployment:

```bash
# Add Prometheus + Grafana
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

### Automation

Create maintenance scripts:

```bash
# Update script
cat > update-nexus.sh <<'EOF'
#!/bin/bash
cd ~/nexus
docker compose pull
docker compose up -d
docker image prune -f
EOF

chmod +x update-nexus.sh
```

## Common Issues

### Models Missing

```bash
# Re-pull models
docker compose exec ollama ollama list
docker compose exec ollama ollama pull llama3.1:8b
```

### Permission Issues

```bash
# Fix bind-mount permissions
docker compose exec gateway chown -R 1000:1000 /var/lib/gateway/data
```

### Network Issues

```bash
# Test connectivity
docker compose exec gateway curl http://ollama:11434/api/tags

# Recreate network
docker compose down
docker network rm nexus_nexus
docker compose up -d
```

### GPU Not Detected

```bash
# Verify NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# Check docker-compose.yml has GPU config
```

## Multi-Host Migration

If you had services on different hosts (ai1, ai2, ada2), you have options:

### Option 1: Single-Host Deployment

Run all services on one powerful host:

```bash
# docker-compose.yml already configured for this
docker compose --profile full up -d
```

### Option 2: Separate Docker Hosts

Keep services on separate hosts, use Docker contexts:

```bash
# On each host, run specific services
# Host 1 (gateway + ollama)
docker compose up -d gateway ollama

# Host 2 (images)
docker compose up -d images

# Update gateway to point to remote services
IMAGES_HTTP_BASE_URL=http://host2:7860
```

### Option 3: Docker Swarm

For true multi-host orchestration:

```bash
# Initialize swarm
docker swarm init

# Join other nodes
docker swarm join --token ...

# Deploy stack
docker stack deploy -c docker-compose.yml nexus
```

## Support

For migration assistance:
- Review [DEPLOYMENT.md](DEPLOYMENT.md)
- Check [troubleshooting section](../README.md#troubleshooting)
- Open GitHub issue
- Join community discussions

## Success Criteria

Migration is successful when:

- [ ] All services healthy (`docker compose ps`)
- [ ] Gateway responds to health checks
- [ ] Can list models via API
- [ ] Chat completions work
- [ ] Image generation works (if enabled)
- [ ] Historical data accessible
- [ ] Performance is acceptable
- [ ] Monitoring is functional
- [ ] Backups are working
- [ ] Old services can be safely stopped
