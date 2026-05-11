FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEXUS_SERVICE_PORT=8610 \
    CUDA_VISIBLE_DEVICES=0

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends bash ca-certificates curl python3.11 python3.11-venv python3-pip \
  && rm -rf /var/lib/apt/lists/* \
  && python3.11 -m venv /opt/venv \
  && ln -sf /opt/venv/bin/python3 /usr/bin/python \
  && ln -sf /opt/venv/bin/pip3 /usr/bin/pip

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY runner/ ./runner/

EXPOSE 8610

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('NEXUS_SERVICE_PORT', '8610'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read(); print('ok')" || exit 1

CMD ["sh", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${NEXUS_SERVICE_PORT:-8610}"]
