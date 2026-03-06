#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANCHOR_NAME="nexus/mlx_allowlist"
ANCHOR_FILE="/etc/pf.anchors/nexus.mlx_allowlist"
PF_CONF="/etc/pf.conf"
PORT="10240"
DRY_RUN="false"
REMOVE="false"

declare -a ALLOW_IPS=("10.10.22.156" "172.28.0.0/16" "127.0.0.1")

usage() {
  cat <<'EOF'
Usage: deploy/scripts/allowlist-mlx-macos.sh [options]

Configure macOS pf firewall to allow MLX port access only from selected client IPs.
Defaults:
  - allow sources: 10.10.22.156, 172.28.0.0/16, 127.0.0.1
  - port: 10240
  - anchor: nexus/mlx_allowlist

Options:
  --allow IP         Add allowed source IP (repeatable)
  --port N           Target TCP port (default: 10240)
  --dry-run          Print actions/rules but do not modify system files
  --remove           Remove installed rules/anchor references
  -h, --help         Show this help

Examples:
  ./deploy/scripts/allowlist-mlx-macos.sh
  ./deploy/scripts/allowlist-mlx-macos.sh --allow 10.10.22.156 --allow 10.10.22.157
  ./deploy/scripts/allowlist-mlx-macos.sh --remove
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow)
      ALLOW_IPS+=("${2:-}")
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --remove)
      REMOVE="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "${OSTYPE:-}" != darwin* ]]; then
  echo "ERROR: This script is for macOS only." >&2
  exit 1
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: Invalid port: $PORT" >&2
  exit 1
fi

if [[ "$REMOVE" == "false" ]]; then
  declare -a validated=()
  for ip in "${ALLOW_IPS[@]}"; do
    [[ -n "$ip" ]] || continue
    if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[1-2][0-9]|3[0-2])$ ]]; then
      validated+=("$ip")
    else
      echo "ERROR: Invalid IPv4 or CIDR source: $ip" >&2
      exit 1
    fi
  done

  if [[ ${#validated[@]} -eq 0 ]]; then
    echo "ERROR: At least one --allow IP is required." >&2
    exit 1
  fi
  ALLOW_IPS=("${validated[@]}")
fi

build_anchor_rules() {
  local list=""
  local ip
  for ip in "${ALLOW_IPS[@]}"; do
    if [[ -n "$list" ]]; then
      list+=" , "
    fi
    list+="$ip"
  done

  cat <<EOF
# Managed by nexus deploy/scripts/allowlist-mlx-macos.sh
# Restrict MLX TCP port to explicit source IPs.
table <nexus_mlx_allow> const { ${list} }
pass in quick proto tcp from <nexus_mlx_allow> to any port ${PORT} keep state
block in quick proto tcp to any port ${PORT}
EOF
}

build_pf_conf_snippet() {
  cat <<EOF
anchor "${ANCHOR_NAME}"
load anchor "${ANCHOR_NAME}" from "${ANCHOR_FILE}"
EOF
}

if [[ "$REMOVE" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "Would remove: ${ANCHOR_FILE}" >&2
    echo "Would remove anchor lines from ${PF_CONF}:" >&2
    build_pf_conf_snippet
    echo "Would run: sudo pfctl -f ${PF_CONF} && sudo pfctl -e" >&2
    exit 0
  fi

  sudo rm -f "${ANCHOR_FILE}"
  sudo sed -i '' '/^anchor "nexus\/mlx_allowlist"$/d' "${PF_CONF}"
  sudo sed -i '' '/^load anchor "nexus\/mlx_allowlist" from "\/etc\/pf\.anchors\/nexus\.mlx_allowlist"$/d' "${PF_CONF}"
  sudo pfctl -f "${PF_CONF}" >/dev/null
  sudo pfctl -e >/dev/null 2>&1 || true

  echo "Removed MLX allowlist rules and reloaded pf."
  exit 0
fi

RULES_CONTENT="$(build_anchor_rules)"
SNIPPET_CONTENT="$(build_pf_conf_snippet)"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Would write ${ANCHOR_FILE} with:" >&2
  echo "$RULES_CONTENT"
  echo
  echo "Would ensure the following lines exist in ${PF_CONF}:" >&2
  echo "$SNIPPET_CONTENT"
  echo
  echo "Would run: sudo pfctl -f ${PF_CONF} && sudo pfctl -e" >&2
  exit 0
fi

TMP_ANCHOR="$(mktemp)"
printf '%s
' "$RULES_CONTENT" > "$TMP_ANCHOR"
sudo install -m 644 "$TMP_ANCHOR" "${ANCHOR_FILE}"
rm -f "$TMP_ANCHOR"

if ! sudo grep -qxF "anchor \"${ANCHOR_NAME}\"" "${PF_CONF}"; then
  echo "anchor \"${ANCHOR_NAME}\"" | sudo tee -a "${PF_CONF}" >/dev/null
fi
if ! sudo grep -qxF "load anchor \"${ANCHOR_NAME}\" from \"${ANCHOR_FILE}\"" "${PF_CONF}"; then
  echo "load anchor \"${ANCHOR_NAME}\" from \"${ANCHOR_FILE}\"" | sudo tee -a "${PF_CONF}" >/dev/null
fi

sudo pfctl -f "${PF_CONF}" >/dev/null
sudo pfctl -e >/dev/null 2>&1 || true

echo "Installed MLX allowlist (port ${PORT}) for IP(s): ${ALLOW_IPS[*]}"
echo "Verify rules: sudo pfctl -sr | grep -E '${PORT}|nexus_mlx_allow'"
echo "Remove later: ./deploy/scripts/allowlist-mlx-macos.sh --remove"
