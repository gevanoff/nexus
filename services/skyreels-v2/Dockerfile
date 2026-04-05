FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends bash ca-certificates curl git ffmpeg build-essential \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY runtime-requirements.txt ./runtime-requirements.txt
COPY app/ ./app/
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 9180

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9180/healthz', timeout=3).read(); print('ok')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
