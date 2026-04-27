FROM python:3.11-slim

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends ca-certificates curl openssh-client \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9190"]
