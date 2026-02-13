FROM python:3.11-slim

# Full-featured Local AI Gateway container.
# NOTE: This Dockerfile is built from the repository root as context (see nexus/docker-compose.gateway.yml).
# It intentionally preserves the known-working runtime layout from ai-infra under /var/lib/gateway/*.

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Runtime root (mirrors ai-infra)
ENV GATEWAY_RUNTIME_ROOT=/var/lib/gateway

WORKDIR /var/lib/gateway

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (pinned)
COPY app/requirements.freeze.txt /tmp/requirements.freeze.txt
RUN pip install --no-cache-dir -r /tmp/requirements.freeze.txt

# App code
COPY app/ /var/lib/gateway/app/

# Convenience: ship gateway tools into the image as well.
COPY tools/ /var/lib/gateway/tools/

# Create runtime dirs used by the app (data + tool logs + tool sandbox)
RUN mkdir -p \
        /var/lib/gateway/data/tools \
        /var/lib/gateway/data/ui_images \
        /var/lib/gateway/data/ui_files \
        /var/lib/gateway/data/ui_chats \
        /var/lib/gateway/data/tools_work \
    && chmod -R 755 /var/lib/gateway

# The default tools registry in the gateway repo uses /var/lib/gateway/tools as cwd.
# In a container, prefer a writable sandbox under /var/lib/gateway/data.
RUN if [ -f "/var/lib/gateway/app/tools_registry.json" ]; then \
            sed -i 's#"cwd"[[:space:]]*:[[:space:]]*"/var/lib/gateway/tools"#"cwd": "/var/lib/gateway/data/tools_work"#g' \
                "/var/lib/gateway/app/tools_registry.json"; \
        fi

EXPOSE 8800 8801

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8800/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
