#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

default_age_key_file() {
  if [[ -n "${SOPS_AGE_KEY_FILE:-}" ]]; then
    printf '%s\n' "$SOPS_AGE_KEY_FILE"
    return 0
  fi
  printf '%s\n' "${HOME}/.config/sops/age/keys.txt"
}

recipient_list_from_key_file() {
  local key_file="$1"
  [[ -f "$key_file" ]] || return 1
  grep '^# public key: ' "$key_file" | sed 's/^# public key: //' | paste -sd, -
}

resolve_secret_file() {
  local explicit_file="$1"
  local environment="$2"
  local scope="$3"
  local host_name="$4"

  if [[ -n "${explicit_file:-}" ]]; then
    printf '%s\n' "$explicit_file"
    return 0
  fi

  case "$scope" in
    common)
      ns_sops_secret_common_file "$ROOT_DIR" "$environment"
      ;;
    host)
      if [[ -z "${host_name:-}" ]]; then
        ns_print_error "--host is required when scope=host."
        exit 2
      fi
      ns_sops_secret_specific_file "$ROOT_DIR" "$environment" "$host_name"
      ;;
    default)
      ns_sops_secret_specific_file "$ROOT_DIR" "$environment" ""
      ;;
    *)
      ns_print_error "Unsupported secret scope: ${scope}"
      exit 2
      ;;
  esac
}

usage() {
  cat <<'EOF'
Usage:
  deploy/scripts/sops-secrets.sh keygen [--age-key-file PATH]
  deploy/scripts/sops-secrets.sh import-dotenv --input PATH --environment <dev|prod> [--host HOST|--common|--default] [--output PATH] [--age-recipient RECIPIENTS] [--age-key-file PATH]
  deploy/scripts/sops-secrets.sh edit --environment <dev|prod> [--host HOST|--common|--default] [--file PATH] [--age-recipient RECIPIENTS] [--age-key-file PATH]
  deploy/scripts/sops-secrets.sh decrypt --environment <dev|prod> [--host HOST|--common|--default] [--file PATH] [--output PATH]
  deploy/scripts/sops-secrets.sh materialize --environment <dev|prod> [--topology-host HOST] [--env-file PATH]

Tracked encrypted secret paths:
  deploy/secrets/<environment>/common.env.sops
  deploy/secrets/<environment>/<host>.env.sops
  deploy/secrets/<environment>/default.env.sops

Generated overlays:
  deploy/env/.env.<environment>[.<host>].sops.common.local
  deploy/env/.env.<environment>[.<host>].sops.local
EOF
}

cmd="${1:-}"
[[ -n "${cmd:-}" ]] || { usage >&2; exit 1; }
shift || true

case "$cmd" in
  keygen)
    age_key_file="$(default_age_key_file)"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --age-key-file)
          age_key_file="${2:-}"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          ns_print_error "Unknown option for keygen: $1"
          usage >&2
          exit 2
          ;;
      esac
    done
    if ! ns_have_cmd age-keygen; then
      ns_print_error "age-keygen is required but not installed."
      exit 1
    fi
    mkdir -p "$(dirname "$age_key_file")"
    if [[ -f "$age_key_file" ]]; then
      chmod 600 "$age_key_file" 2>/dev/null || true
      ns_print_ok "Age key already present at ${age_key_file}"
    else
      age-keygen -o "$age_key_file"
      chmod 600 "$age_key_file" 2>/dev/null || true
      ns_print_ok "Created new SOPS age key at ${age_key_file}"
    fi
    recipients="$(recipient_list_from_key_file "$age_key_file" || true)"
    if [[ -n "${recipients:-}" ]]; then
      printf 'SOPS_AGE_KEY_FILE=%s\n' "$age_key_file"
      printf 'NEXUS_SOPS_AGE_RECIPIENTS=%s\n' "$recipients"
    fi
    ;;

  import-dotenv)
    input_file=""
    output_file=""
    environment=""
    scope="host"
    host_name=""
    age_recipient="${NEXUS_SOPS_AGE_RECIPIENTS:-}"
    age_key_file="$(default_age_key_file)"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --input)
          input_file="${2:-}"
          shift 2
          ;;
        --output|--file)
          output_file="${2:-}"
          shift 2
          ;;
        --environment)
          environment="${2:-}"
          shift 2
          ;;
        --host)
          scope="host"
          host_name="${2:-}"
          shift 2
          ;;
        --common)
          scope="common"
          shift
          ;;
        --default)
          scope="default"
          shift
          ;;
        --age-recipient)
          age_recipient="${2:-}"
          shift 2
          ;;
        --age-key-file)
          age_key_file="${2:-}"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          ns_print_error "Unknown option for import-dotenv: $1"
          usage >&2
          exit 2
          ;;
      esac
    done
    [[ -n "${input_file:-}" && -f "$input_file" ]] || { ns_print_error "--input must point to an existing dotenv file."; exit 1; }
    [[ -n "${environment:-}" || -n "${output_file:-}" ]] || { ns_print_error "--environment or --output is required."; exit 1; }
    if ! ns_have_cmd sops; then
      ns_print_error "sops is required but not installed."
      exit 1
    fi
    if [[ -z "${age_recipient:-}" ]]; then
      age_recipient="$(recipient_list_from_key_file "$age_key_file" || true)"
    fi
    [[ -n "${age_recipient:-}" ]] || { ns_print_error "No age recipient available. Pass --age-recipient or run keygen first."; exit 1; }
    secret_file="$(resolve_secret_file "$output_file" "$environment" "$scope" "$host_name")"
    mkdir -p "$(dirname "$secret_file")"
    tmp_output="$(mktemp "${secret_file}.tmp.XXXXXX")"
    sops --encrypt --input-type dotenv --output-type dotenv --age "$age_recipient" "$input_file" >"$tmp_output"
    chmod 600 "$tmp_output" 2>/dev/null || true
    mv "$tmp_output" "$secret_file"
    ns_print_ok "Wrote encrypted dotenv to ${secret_file}"
    ;;

  edit)
    output_file=""
    environment=""
    scope="host"
    host_name=""
    age_recipient="${NEXUS_SOPS_AGE_RECIPIENTS:-}"
    age_key_file="$(default_age_key_file)"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --file|--output)
          output_file="${2:-}"
          shift 2
          ;;
        --environment)
          environment="${2:-}"
          shift 2
          ;;
        --host)
          scope="host"
          host_name="${2:-}"
          shift 2
          ;;
        --common)
          scope="common"
          shift
          ;;
        --default)
          scope="default"
          shift
          ;;
        --age-recipient)
          age_recipient="${2:-}"
          shift 2
          ;;
        --age-key-file)
          age_key_file="${2:-}"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          ns_print_error "Unknown option for edit: $1"
          usage >&2
          exit 2
          ;;
      esac
    done
    [[ -n "${environment:-}" || -n "${output_file:-}" ]] || { ns_print_error "--environment or --file is required."; exit 1; }
    if ! ns_have_cmd sops; then
      ns_print_error "sops is required but not installed."
      exit 1
    fi
    secret_file="$(resolve_secret_file "$output_file" "$environment" "$scope" "$host_name")"
    mkdir -p "$(dirname "$secret_file")"
    if [[ ! -f "$secret_file" ]]; then
      if [[ -z "${age_recipient:-}" ]]; then
        age_recipient="$(recipient_list_from_key_file "$age_key_file" || true)"
      fi
      [[ -n "${age_recipient:-}" ]] || { ns_print_error "Cannot initialize ${secret_file} without an age recipient."; exit 1; }
      tmp_input="$(mktemp "${secret_file}.seed.XXXXXX")"
      : >"$tmp_input"
      sops --encrypt --input-type dotenv --output-type dotenv --age "$age_recipient" "$tmp_input" >"$secret_file"
      rm -f "$tmp_input"
      chmod 600 "$secret_file" 2>/dev/null || true
      ns_print_ok "Initialized empty encrypted dotenv at ${secret_file}"
    fi
    sops "$secret_file"
    ;;

  decrypt)
    output_file=""
    destination=""
    environment=""
    scope="host"
    host_name=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --file)
          output_file="${2:-}"
          shift 2
          ;;
        --output)
          destination="${2:-}"
          shift 2
          ;;
        --environment)
          environment="${2:-}"
          shift 2
          ;;
        --host)
          scope="host"
          host_name="${2:-}"
          shift 2
          ;;
        --common)
          scope="common"
          shift
          ;;
        --default)
          scope="default"
          shift
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          ns_print_error "Unknown option for decrypt: $1"
          usage >&2
          exit 2
          ;;
      esac
    done
    [[ -n "${environment:-}" || -n "${output_file:-}" ]] || { ns_print_error "--environment or --file is required."; exit 1; }
    if ! ns_have_cmd sops; then
      ns_print_error "sops is required but not installed."
      exit 1
    fi
    secret_file="$(resolve_secret_file "$output_file" "$environment" "$scope" "$host_name")"
    [[ -f "$secret_file" ]] || { ns_print_error "Secret file not found: ${secret_file}"; exit 1; }
    if [[ -n "${destination:-}" ]]; then
      mkdir -p "$(dirname "$destination")"
      sops --decrypt --input-type dotenv --output-type dotenv "$secret_file" >"$destination"
      chmod 600 "$destination" 2>/dev/null || true
      ns_print_ok "Decrypted ${secret_file} to ${destination}"
    else
      sops --decrypt --input-type dotenv --output-type dotenv "$secret_file"
    fi
    ;;

  materialize)
    exec "$ROOT_DIR/deploy/scripts/materialize-sops-env.sh" "$@"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    ns_print_error "Unknown command: ${cmd}"
    usage >&2
    exit 2
    ;;
esac
