# Topology Manifests

These files are the desired-state source of truth for multi-host Nexus placement.

Use topology manifests for:

- which host owns which components
- which host-specific env overrides should be materialized
- which host is expected to run native services outside Compose
- which SSH target and checkout path are canonical for a host profile

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
