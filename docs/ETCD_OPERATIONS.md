# Etcd Operations

This document defines the current Nexus etcd operating model.

## Scope

Today, etcd should be treated as:

- a multi-host service registry
- a place for small cluster-scoped operational flags
- a coordination layer for discovery and maintenance hints

It should not yet be treated as the sole source of truth for all runtime configuration.

Keep durable operator-owned configuration in Git and host env/config files. Use etcd for dynamic state.

## Key Layout

Recommended key layout:

- `/nexus/services/<service-name>`
  Purpose: canonical service registration record
- `/nexus/hosts/<host-name>/status`
  Purpose: coarse host status published by operators or automation
- `/nexus/routing/drain/hosts/<host-name>`
  Purpose: temporary host drain switch during maintenance or rollout
- `/nexus/routing/drain/services/<service-name>`
  Purpose: temporary service drain switch during rollout
- `/nexus/routing/hints/<hint-name>`
  Purpose: optional cluster-wide routing hints

Service registration value shape:

```json
{
  "name": "skyreels-v2",
  "base_url": "http://ai1:9180",
  "metadata_url": "http://ai1:9180/v1/metadata"
}
```

Optional host status value shape:

```json
{
  "host": "ada2",
  "status": "ready",
  "updated_at": "2026-03-09T20:15:00Z",
  "notes": "heartmula rollout complete"
}
```

Optional drain value shape:

```json
{
  "enabled": true,
  "reason": "rolling restart",
  "updated_at": "2026-03-09T20:20:00Z"
}
```

## Cluster Bootstrap

The etcd compose component is now cluster-capable through `ETCD_*` env settings.

For each host, set:

- `ETCD_NAME`
- `ETCD_ADVERTISE_CLIENT_URLS`
- `ETCD_INITIAL_ADVERTISE_PEER_URLS`
- `ETCD_INITIAL_CLUSTER`
- `ETCD_INITIAL_CLUSTER_STATE`
- `ETCD_INITIAL_CLUSTER_TOKEN`

Use the helper script to write those values into the host env file.

If you want a 3-member cluster across `ai1`, `ai2`, and `ada2`, all three hosts must use the same:

- `ETCD_INITIAL_CLUSTER`
- `ETCD_INITIAL_CLUSTER_TOKEN`
- `ETCD_INITIAL_CLUSTER_STATE`

For a brand-new 3-member bootstrap, all three members must start from empty data dirs.
If one host still has old etcd data, the fresh members will report cluster ID mismatch and remain unhealthy.

Example 3-member cluster string:

```text
ai1-etcd=http://ai1:2380,ai2-etcd=http://ai2:2380,ada2-etcd=http://ada2:2380
```

Example on `ai1`:

```bash
./deploy/scripts/init-etcd-cluster.sh \
  --name ai1-etcd \
  --client-url http://ai1:2379 \
  --peer-url http://ai1:2380 \
  --initial-cluster ai1-etcd=http://ai1:2380,ai2-etcd=http://ai2:2380,ada2-etcd=http://ada2:2380
```

Run the equivalent command on `ai2` and `ada2` with their own member names and URLs.

Keep `ETCD_URL` separate from cluster member advertisement.
If gateway and etcd run in the same compose stack on a host, leave `ETCD_URL` pointed at the local compose service, for example `http://etcd:2379`.

Then start etcd on each host.
For a brand-new cluster, order is not important as long as all intended members are configured with the same cluster string and token:

```bash
docker compose -f docker-compose.etcd.yml up -d
```

For a coordinated clean bootstrap across multiple hosts, use:

```bash
./deploy/scripts/bootstrap-etcd-cluster.sh \
  --leader ai2-etcd \
  --member ai1-etcd,ai1,ai@ai1 \
  --member ai2-etcd,ai2,ai@ai2 \
  --member ada2-etcd,ada2,ai@ada2
```

In this script, `--leader` means bootstrap coordinator and final health-check host.
It does not force etcd to elect a particular raft leader.

## Health Checks

Use the health helper:

```bash
./deploy/scripts/check-etcd-health.sh
```

This checks:

- endpoint health
- endpoint status
- member list

## Recovering An Unhealthy Member

When one member is unhealthy, check these in order:

1. container logs
2. member env values in `.env`
3. peer reachability on port `2380`
4. whether the local data dir belongs to an older or mismatched cluster state

Useful commands on the affected host:

```bash
docker logs --tail 200 nexus-etcd
```

```bash
./deploy/scripts/check-etcd-health.sh
```

```bash
grep '^ETCD_' .env
```

Common cases:

- Fresh bootstrap, no important data yet:
  stop etcd on every member that belongs to that cluster, remove each member's `./.runtime/etcd/data`, rerun `init-etcd-cluster.sh` with the same cluster string on each host, and start them again.
- Existing cluster, member data is corrupt or mismatched:
  restore from snapshot or remove/re-add the member with `etcdctl member remove` and `member add` before restarting it.
- Peer connectivity failure:
  confirm both hosts can reach each other on `2380`; etcd peer traffic must work in both directions.

Full rebuild note:

- If `ai2` is part of the cluster, wiping only `ai1` and `ada2` is not a full rebuild.
- A surviving `ai2` member will keep its old cluster ID, and the wiped members will fail to join it if they were bootstrapped as a different new cluster.
- A true clean rebuild requires stopping and wiping all members that appear in `ETCD_INITIAL_CLUSTER`.

Ownership note:

- `./.runtime/etcd/data` becoming `root:root` is expected with the current compose setup because the etcd container writes that directory as root.
- That ownership alone does not mean the member is unhealthy.
- If you need to delete or move the data dir during recovery, use `sudo` on the host.

Important:

- A 2-member etcd cluster is not fault-tolerant. If either member is down, quorum is at risk.
- For durable production use, prefer 3 members.

## Backup

Create a point-in-time snapshot:

```bash
./deploy/scripts/backup-etcd.sh
```

Backups are stored by default under:

```text
.runtime/etcd/backups/
```

## Restore

Restore a snapshot into the local host's etcd data directory:

```bash
./deploy/scripts/restore-etcd.sh --snapshot ./.runtime/etcd/backups/etcd-snapshot-YYYYMMDD-HHMMSS.db
```

The restore script:

- stops the etcd container
- moves the existing data directory aside
- restores the snapshot into the compose-mounted data dir
- restarts etcd

## Service Registration

Register a service manually:

```bash
./deploy/scripts/register-service.sh skyreels-v2 http://ai1:9180 http://ai1:2379
```

List registered services:

```bash
./deploy/scripts/list-services.sh http://ai1:2379
```

## Operational Guidance

Use etcd first for:

- service discovery
- drain flags
- host readiness markers
- routing hints

Do not use etcd as the only home for:

- secrets
- large config documents
- model alias source files
- irreplaceable operator config