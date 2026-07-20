FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
       bash build-essential ca-certificates curl ffmpeg git git-lfs \
       libgl1 libglib2.0-0 python3 python3-dev python3-pip python3-venv \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m pip install --break-system-packages --no-cache-dir \
      fastapi==0.110.3 \
      httpx==0.27.0 \
      uv==0.8.2 \
      'uvicorn[standard]==0.29.0'

COPY app/ /app/app/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 9180 9185 9195

ENTRYPOINT ["/app/docker-entrypoint.sh"]
