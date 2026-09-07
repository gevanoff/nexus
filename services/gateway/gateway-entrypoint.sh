#!/bin/sh
set -eu

CGROUP_ROOT="/sys/fs/cgroup"
SERVICE_CGROUP="${CGROUP_ROOT}/nexus-gateway-service"

if [ "$(id -u)" -ne 0 ]; then
    echo "gateway startup requires root to prepare validation cgroup delegation" >&2
    exit 1
fi
if [ ! -f "${CGROUP_ROOT}/cgroup.controllers" ]; then
    echo "cgroup v2 is required for validation containment" >&2
    exit 1
fi

# Docker mounts the container's private cgroup namespace read-only. The service
# receives CAP_SYS_ADMIN only long enough to make that private mount writable and
# delegate memory/pids controllers. The capability is removed permanently before
# the Gateway starts.
mount -o remount,rw "${CGROUP_ROOT}"
mkdir -p "${SERVICE_CGROUP}"
echo $$ > "${SERVICE_CGROUP}/cgroup.procs"
echo "+memory +pids" > "${CGROUP_ROOT}/cgroup.subtree_control"

export NEXUS_VALIDATION_CGROUP_ROOT="${CGROUP_ROOT}"
exec setpriv \
    --no-new-privs \
    --bounding-set=-sys_admin,-setpcap \
    --inh-caps=-sys_admin,-setpcap \
    --ambient-caps=-sys_admin,-setpcap \
    -- "$@"
