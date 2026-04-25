FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/var/lib/mlx/cache/huggingface \
    XDG_CACHE_HOME=/var/lib/mlx/cache

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG MLX_PIP_PACKAGES="mlx-openai-server"
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir ${MLX_PIP_PACKAGES}

EXPOSE 10240

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD curl -fsS http://localhost:10240/v1/models >/dev/null || exit 1

CMD ["sh", "-lc", "if [ -n \"${MLX_CONFIG_PATH:-}\" ]; then mlx-openai-server launch --config \"${MLX_CONFIG_PATH}\"; else mlx-openai-server launch --model-path \"${MLX_MODEL_PATH:-mlx-community/Qwen3-30B-A3B-4bit}\" --model-type \"${MLX_MODEL_TYPE:-lm}\" --host \"${MLX_HOST:-0.0.0.0}\" --port \"${MLX_PORT:-10240}\"; fi"]
