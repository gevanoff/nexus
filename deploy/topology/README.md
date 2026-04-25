# Topology Manifests

These files are the desired-state source of truth for multi-host Nexus placement.

Use topology manifests for:

- which host owns which components
- which host-specific env overrides should be materialized
- which host is expected to run native services outside Compose
- which SSH target and checkout path are canonical for a host profile
- which host platform groups should drive per-hosttype defaults

Do not treat etcd as the deployment plan. etcd is the live runtime registry:

- service registrars publish health-checked endpoints into etcd
- the gateway reads etcd to discover currently available backends
- etcd should reflect deployed state, not replace the versioned plan

Current tracked topology:

- `production.json`: canonical placement for `ai1`, `ai2`, and `ada2`

Typical workflow:

1. Materialize a host env file from the topology manifest.
2. Deploy that host with `deploy.sh --topology-host <host> ...`.
3. Optionally deploy the same host remotely with `remote-deploy.sh --topology-host <host> ...`.
4. Let service registrars populate etcd from the deployed services.

## Routine Backend Moves

Use the helper when a backend family needs to move between tracked hosts:

```bash
./deploy/scripts/reassign-topology-family.sh --family vllm --from ai2 --to ada2 --write
./deploy/scripts/reassign-topology-family.sh --family tts --from ai1 --to ai2 --write
./deploy/scripts/reassign-topology-family.sh --family qwen3-tts --from ai1 --to ai2 --components-mode ignore --write
```

Supported families today:

- `vllm`
- `tts`
- `qwen3-tts`

Recommended rollout order after a topology move:

1. Deploy the destination host first.
2. Deploy any gateway host next so backend URLs refresh in rendered env files.
3. Deploy the source host last so old components are removed.
4. Verify Gateway and upstream health, then re-register services if needed.

Compatibility note:

- The current vLLM deploy path can be assigned either as the monolithic `vllm` profile or as split lanes: `vllm-strong`, `vllm-fast`, and `vllm-embeddings`.
- All vLLM profiles are GPU-bound (`docker-compose.vllm*.yml` uses `gpus: all`), so they should only be assigned to GPU-capable hosts.
- The tracked `vllm` defaults may require Hugging Face auth or higher rate limits, so set `HUGGING_FACE_HUB_TOKEN` on the destination host when needed.
