# MLX Service

Containerized MLX OpenAI-compatible server component for Nexus.

## Placement Policy

- MLX must run host-native on macOS bare metal for Apple Silicon acceleration.
- CPU-only backends that do not benefit from NVIDIA acceleration should run as containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated workloads should run on Linux/NVIDIA hosts.

## Platform Compatibility

`mlx-openai-server` requires **macOS on Apple Silicon (M-series)**. Docker containers in Nexus run Linux userspace/kernel semantics, so this component can fail to start and appear in a restart loop on unsupported environments.

If you see restart-loop behavior for `nexus-mlx`, this is usually a runtime/platform mismatch rather than a Gateway routing issue.

## Status

⚠️ Initial Nexus port scaffold (migration in progress).

This component ports MLX service execution into Nexus compose/component patterns so Gateway can target `http://mlx:10240/v1` on the internal network.

## Configuration

See `env/mlx.env.example` for primary variables:

- `MLX_PORT` (default `10240`)
- `MLX_MODEL_PATH` (default `mlx-community/gemma-2-2b-it-8bit`)
- `MLX_MODEL_TYPE` (default `lm`)

## Compose usage

```bash
# Start with core stack + MLX component
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml -f docker-compose.mlx.yml up -d

# Check health
curl -sS http://localhost:10240/v1/models
```

## Notes

- Gateway should use `MLX_BASE_URL=http://mlx:10240/v1` when the MLX component is enabled.
- MLX model/runtime compatibility depends on host/container environment and chosen model.

Install host-native MLX on macOS with:

```bash
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

Installer prerequisites:

- Python `>=3.11` is required for current `mlx-openai-server` builds.
- If your default `python3` is older (for example macOS system Python 3.9), install a newer one and pin it for install:

```bash
MLX_PYTHON=/opt/homebrew/bin/python3.12 ./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

## Troubleshooting Restart Loops

1. Remove `-f docker-compose.mlx.yml` from your compose invocation on Linux/Windows hosts.
2. Run MLX natively on an Apple Silicon macOS host.
3. Point Gateway at that host by setting `MLX_BASE_URL` in `nexus/.env`.

Example:

```bash
MLX_BASE_URL=http://<mac-host-or-ip>:10240/v1
```

Container-to-native migration quick path:

```bash
# 1) Install/start native MLX on macOS
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240

# 2) Verify local health on macOS host
curl -fsS http://127.0.0.1:10240/v1/models

# 3) Update nexus/.env
# MLX_BASE_URL=http://host.docker.internal:10240/v1

# 4) Start Nexus without mlx container
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d --build

# 5) Verify gateway contract using external/native MLX
./deploy/scripts/verify-gateway.sh --with-mlx --external-mlx
```

## Security Baseline (Native MLX Host)

- Run MLX under a dedicated non-admin service account.
- Prefer loopback-only binding and publish externally only through a constrained reverse proxy.
- Restrict ingress to Gateway/control-plane source IPs with host firewall rules.
- Keep model/cache paths owned by the service account with least-privilege permissions.
