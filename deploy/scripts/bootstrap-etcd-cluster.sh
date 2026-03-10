#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"
REPO_DIR="${NEXUS_REMOTE_DIR:-__AUTO__}"
ENV_FILE_REL=".env"
CLUSTER_TOKEN="nexus-etcd-cluster"
DO_WIPE="true"
START_CLUSTER="true"
CHECK_HEALTH="true"
LEADER_NAME=""
MEMBER_SPECS=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/bootstrap-etcd-cluster.sh --leader MEMBER --member NAME,HOST,SSH [--member NAME,HOST,SSH ...] [options]

Performs a clean etcd cluster bootstrap across multiple hosts over SSH.
The designated leader is only the bootstrap coordinator / final health-check host.
It is not a forced etcd raft leader.

Required:
  --leader NAME                Member name to use as coordinator (must match one --member NAME)
  --member NAME,HOST,SSH       Repeat once per cluster member
                               Example: --member ai1-etcd,ai1,ai@ai1

Options:
  --yes                        Non-interactive SSH mode
  --repo-dir PATH              Remote Nexus repo dir (default: auto-detect ~/ai/nexus, ~/nexus, /Users/ai/nexus)
  --env-file RELPATH           Env file relative to repo dir (default: .env)
  --cluster-token TOKEN        Shared etcd cluster token
  --no-wipe                    Do not clear remote .runtime/etcd/data before bootstrap
  --no-start                   Only write env files; do not start etcd
  --no-health-check            Skip final health checks

Example:
  ./deploy/scripts/bootstrap-etcd-cluster.sh \
    --leader ai2-etcd \
    --member ai1-etcd,ai1,ai@ai1 \
    --member ai2-etcd,ai2,ai@ai2 \
    --member ada2-etcd,ada2,ai@ada2
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      NS_AUTO_YES="true"
      shift
      ;;
    --leader)
      LEADER_NAME="${2:-}"
      shift 2
      ;;
    --member)
      MEMBER_SPECS+=("${2:-}")
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE_REL="${2:-}"
      shift 2
      ;;
    --cluster-token)
      CLUSTER_TOKEN="${2:-}"
      shift 2
      ;;
    --no-wipe)
      DO_WIPE="false"
      shift
      ;;
    --no-start)
      START_CLUSTER="false"
      shift
      ;;
    --no-health-check)
      CHECK_HEALTH="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

[[ -n "$LEADER_NAME" ]] || ns_die "--leader is required"
(( ${#MEMBER_SPECS[@]} >= 1 )) || ns_die "At least one --member is required"

ns_ensure_prereqs false false false false false true || true

ssh_opts=("-o" "StrictHostKeyChecking=accept-new")
if [[ "$NS_AUTO_YES" == "true" ]]; then
  ssh_opts+=("-o" "BatchMode=yes")
else
  ssh_opts+=("-o" "BatchMode=no")
fi

member_names=()
member_hosts=()
member_ssh=()

for spec in "${MEMBER_SPECS[@]}"; do
  IFS=',' read -r member_name advertise_host ssh_target <<<"$spec"
  [[ -n "${member_name:-}" && -n "${advertise_host:-}" && -n "${ssh_target:-}" ]] || ns_die "Invalid --member format: $spec"
  [[ "$member_name" =~ ^[a-zA-Z0-9._-]+$ ]] || ns_die "Invalid member name: $member_name"
  [[ "$advertise_host" =~ ^[a-zA-Z0-9._-]+$ ]] || ns_die "Invalid advertise host: $advertise_host"
  [[ "$ssh_target" =~ ^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$ ]] || ns_die "Invalid SSH target: $ssh_target"
  member_names+=("$member_name")
  member_hosts+=("$advertise_host")
  member_ssh+=("$ssh_target")
done

leader_index="-1"
for i in "${!member_names[@]}"; do
  if [[ "${member_names[$i]}" == "$LEADER_NAME" ]]; then
    leader_index="$i"
    break
  fi
done
[[ "$leader_index" != "-1" ]] || ns_die "--leader must match one of the --member names"

local_hostnames=()
while IFS= read -r candidate; do
  [[ -n "${candidate:-}" ]] || continue
  local_hostnames+=("$candidate")
done < <(
  {
    hostname 2>/dev/null || true
    hostname -s 2>/dev/null || true
    hostname -f 2>/dev/null || true
  } | awk 'NF {print tolower($0)}' | sort -u
)

is_local_target() {
  local ssh_target="$1"
  local advertise_host="$2"
  local ssh_host="${ssh_target#*@}"
  local candidate

  case "${ssh_host,,}" in
    localhost|127.0.0.1|::1)
      return 0
      ;;
  esac
  case "${advertise_host,,}" in
    localhost|127.0.0.1|::1)
      return 0
      ;;
  esac

  for candidate in "${local_hostnames[@]}"; do
    [[ "${ssh_host,,}" == "$candidate" ]] && return 0
    [[ "${advertise_host,,}" == "$candidate" ]] && return 0
  done
  return 1
}

initial_cluster=""
for i in "${!member_names[@]}"; do
  entry="${member_names[$i]}=http://${member_hosts[$i]}:2380"
  if [[ -z "$initial_cluster" ]]; then
    initial_cluster="$entry"
  else
    initial_cluster+=",$entry"
  fi
done

remote_prepare=$(cat <<'EOS'
set -euo pipefail
repo_dir="$1"
env_file_rel="$2"
member_name="$3"
client_url="$4"
peer_url="$5"
initial_cluster="$6"
cluster_token="$7"
do_wipe="$8"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

resolve_repo_dir() {
  local requested="$1"
  if [[ -n "$requested" && "$requested" != "__AUTO__" ]]; then
    printf '%s\n' "$requested"
    return 0
  fi

  local candidates=(
    "$HOME/ai/nexus"
    "$HOME/nexus"
    "/Users/ai/nexus"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate/.git" || -f "$candidate/docker-compose.etcd.yml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "ERROR: Unable to locate Nexus repo on remote host." >&2
  echo "Checked: ${candidates[*]}" >&2
  echo "Pass --repo-dir PATH or set NEXUS_REMOTE_DIR." >&2
  exit 1
}

repo_dir="$(resolve_repo_dir "$repo_dir")"

cd "$repo_dir"
env_file="$repo_dir/$env_file_rel"

source "$repo_dir/deploy/scripts/_common.sh"

if [[ "$do_wipe" == "true" ]]; then
  ns_compose --env-file "$env_file" -f docker-compose.etcd.yml down || true
  docker run --rm -v "$repo_dir/.runtime/etcd/data:/etcd-data" busybox:1.36 sh -c 'rm -rf /etcd-data/* /etcd-data/.[!.]* /etcd-data/..?* 2>/dev/null || true'
fi

./deploy/scripts/init-etcd-cluster.sh \
  --env-file "$env_file" \
  --name "$member_name" \
  --client-url "$client_url" \
  --peer-url "$peer_url" \
  --initial-cluster "$initial_cluster" \
  --cluster-state new \
  --cluster-token "$cluster_token"
EOS
)

remote_start=$(cat <<'EOS'
set -euo pipefail
repo_dir="$1"
env_file_rel="$2"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

resolve_repo_dir() {
  local requested="$1"
  if [[ -n "$requested" && "$requested" != "__AUTO__" ]]; then
    printf '%s\n' "$requested"
    return 0
  fi

  local candidates=(
    "$HOME/ai/nexus"
    "$HOME/nexus"
    "/Users/ai/nexus"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate/.git" || -f "$candidate/docker-compose.etcd.yml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "ERROR: Unable to locate Nexus repo on remote host." >&2
  echo "Pass --repo-dir PATH or set NEXUS_REMOTE_DIR." >&2
  exit 1
}

repo_dir="$(resolve_repo_dir "$repo_dir")"

cd "$repo_dir"
env_file="$repo_dir/$env_file_rel"
source "$repo_dir/deploy/scripts/_common.sh"
ns_compose --env-file "$env_file" -f docker-compose.etcd.yml up -d
EOS
)

remote_health=$(cat <<'EOS'
set -euo pipefail
repo_dir="$1"
env_file_rel="$2"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

resolve_repo_dir() {
  local requested="$1"
  if [[ -n "$requested" && "$requested" != "__AUTO__" ]]; then
    printf '%s\n' "$requested"
    return 0
  fi

  local candidates=(
    "$HOME/ai/nexus"
    "$HOME/nexus"
    "/Users/ai/nexus"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate/.git" || -f "$candidate/docker-compose.etcd.yml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "ERROR: Unable to locate Nexus repo on remote host." >&2
  echo "Pass --repo-dir PATH or set NEXUS_REMOTE_DIR." >&2
  exit 1
}

repo_dir="$(resolve_repo_dir "$repo_dir")"

cd "$repo_dir"
env_file="$repo_dir/$env_file_rel"
./deploy/scripts/check-etcd-health.sh --env-file "$env_file"
EOS
)

run_remote_script() {
  local ssh_target="$1"
  local script_body="$2"
  shift 2
  ssh "${ssh_opts[@]}" "$ssh_target" bash -s -- "$@" <<<"$script_body"
}

run_member_script() {
  local ssh_target="$1"
  local advertise_host="$2"
  local script_body="$3"
  shift 3
  if is_local_target "$ssh_target" "$advertise_host"; then
    bash -s -- "$@" <<<"$script_body"
    return $?
  fi
  if ! ns_have_cmd ssh; then
    ns_die "ssh is required for remote target: $ssh_target"
  fi
  run_remote_script "$ssh_target" "$script_body" "$@"
}

ns_print_header "Preparing etcd members"
for i in "${!member_names[@]}"; do
  if is_local_target "${member_ssh[$i]}" "${member_hosts[$i]}"; then
    ns_print_ok "Preparing ${member_names[$i]} locally"
  else
    ns_print_ok "Preparing ${member_names[$i]} on ${member_ssh[$i]}"
  fi
  run_member_script "${member_ssh[$i]}" "${member_hosts[$i]}" "${remote_prepare}" \
    "$REPO_DIR" "$ENV_FILE_REL" "${member_names[$i]}" "http://${member_hosts[$i]}:2379" "http://${member_hosts[$i]}:2380" "$initial_cluster" "$CLUSTER_TOKEN" "$DO_WIPE"
done

if [[ "$START_CLUSTER" == "true" ]]; then
  ns_print_header "Starting etcd cluster"
  for i in "${!member_names[@]}"; do
    if is_local_target "${member_ssh[$i]}" "${member_hosts[$i]}"; then
      ns_print_ok "Starting ${member_names[$i]} locally"
    else
      ns_print_ok "Starting ${member_names[$i]} on ${member_ssh[$i]}"
    fi
    run_member_script "${member_ssh[$i]}" "${member_hosts[$i]}" "${remote_start}" "$REPO_DIR" "$ENV_FILE_REL"
  done
fi

if [[ "$CHECK_HEALTH" == "true" && "$START_CLUSTER" == "true" ]]; then
  ns_print_header "Checking health from coordinator"
  run_member_script "${member_ssh[$leader_index]}" "${member_hosts[$leader_index]}" "${remote_health}" "$REPO_DIR" "$ENV_FILE_REL"
fi

ns_print_ok "Bootstrap configuration complete"
echo "Coordinator: ${LEADER_NAME} (${member_ssh[$leader_index]})"
echo "Cluster: ${initial_cluster}"