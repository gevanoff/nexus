# Historical ai-infra Migration

The original ai-infra-to-Nexus migration is complete for the tracked cluster.
The old automated migration scripts were removed because they assumed a
single-host Ollama stack, invoked Compose without explicit manifests, and
restored Gateway state into paths that are no longer used.

Do not use old copies of these retired scripts:

- `deploy/scripts/migrate-from-ai-infra.sh`
- `deploy/scripts/backup-and-deploy-parallel.sh`
- `deploy/scripts/cutover-one-way.sh`

## Current Replacement

For a new or rebuilt tracked host, use the topology as the source of truth:

```bash
./deploy/scripts/deploy.sh --topology-host <host> prod main
```

From another machine:

```bash
./deploy/scripts/remote-deploy.sh --topology-host <host> prod main
```

Use `quickstart.sh` only for a local single-host development installation.

## Data Recovery

Current persistent state lives under the host's configured
`NEXUS_RUNTIME_ROOT`. Use the maintained, data-specific tools instead of a
general migration script:

- `backup-gateway-db.sh` and `restore-gateway-db.sh`
- `backup-etcd.sh` and `restore-etcd.sh`
- `seed-tts-refs.sh` for reference-audio migration
- `purge-mlx-model-cache.sh` for a guarded MLX cache reset

Gateway operator configuration is materialized under
`${NEXUS_RUNTIME_ROOT}/gateway/config`; read-write Gateway state is under
`${NEXUS_RUNTIME_ROOT}/gateway/data`.

The old TTS launchd cutover is also complete. Use `seed-tts-refs.sh` for data
import and topology deployment for service placement.

See [deploy/SCRIPTS.md](../deploy/SCRIPTS.md) for the supported operational
entry points and script lifecycle classifications.
