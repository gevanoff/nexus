# MLX Service

Containerized MLX OpenAI-compatible server component for Nexus.

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
