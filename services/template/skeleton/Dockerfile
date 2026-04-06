ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEXUS_SERVICE_PORT=__PORT__

ARG EXTRA_APT_PACKAGES=""

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends bash ca-certificates curl ${EXTRA_APT_PACKAGES} \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE __PORT__

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('NEXUS_SERVICE_PORT', '__PORT__'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read(); print('ok')" || exit 1

CMD ["sh", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${NEXUS_SERVICE_PORT:-__PORT__}"]
