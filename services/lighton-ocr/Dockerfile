FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG AI_TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends bash ca-certificates curl git build-essential \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
  && pip install --no-cache-dir --index-url ${AI_TORCH_INDEX_URL} --extra-index-url https://pypi.org/simple torch torchvision torchaudio

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 9155

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9155/health', timeout=3).read(); print('ok')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9155"]