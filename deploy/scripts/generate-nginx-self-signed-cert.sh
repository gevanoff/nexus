#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERT_DIR="${ROOT_DIR}/.runtime/nginx/certs"
DAYS="${DAYS:-365}"
COMMON_NAME="${COMMON_NAME:-localhost}"

mkdir -p "${CERT_DIR}"

openssl req -x509 -nodes -newkey rsa:4096 \
  -keyout "${CERT_DIR}/privkey.pem" \
  -out "${CERT_DIR}/fullchain.pem" \
  -days "${DAYS}" \
  -subj "/CN=${COMMON_NAME}" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

chmod 600 "${CERT_DIR}/privkey.pem" "${CERT_DIR}/fullchain.pem"

echo "Generated self-signed certs in: ${CERT_DIR}"
echo "- fullchain.pem"
echo "- privkey.pem"
