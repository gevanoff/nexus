# Infrastructure Script Map

This file is the authoritative map for `deploy/scripts`. The tracked production
topology in `deploy/topology/production.json` is the desired-state source of
truth; scripts must not independently invent production placement.
`deploy.sh` is the only host deployment engine.

## Command Hierarchy

1. Use `deploy.sh --topology-host HOST prod BRANCH` for a deployment from the
   target host.
2. Use `remote-deploy.sh --topology-host HOST prod BRANCH` from another machine.
3. Use `ansible-topology.sh` for repeatable multi-host bootstrap or deployment.
4. Use a focused `restart-*` or `redeploy-*` helper only when placement and
   configuration are already correct and one service needs to be recycled.
5. Use `quickstart.sh` for local development, not for tracked production hosts.

`ops-stack.sh` is only a guarded convenience wrapper around `deploy.sh`. It has
no Compose, Git, env-materialization, or verification implementation of its own.

## Deployment And Topology

| Script | Role | Lifecycle |
|---|---|---|
| `deploy.sh` | Canonical host-local deployment engine; pulls code, renders topology/env, preflights, resolves bind paths, and runs selected Compose manifests | Supported |
| `remote-deploy.sh` | Resolves/copies controller inputs and invokes `deploy.sh` over SSH | Supported |
| `ops-stack.sh` | Compatibility/convenience wrapper that requires a topology host and delegates to `deploy.sh` | Supported wrapper |
| `ansible-topology.sh` | Thin CLI for topology-backed Ansible inventory/bootstrap/deploy/site playbooks | Supported |
| `render-topology-env.sh` | Materializes one host's tracked topology env | Supported internal/operator tool |
| `reassign-topology-family.sh` | Moves a backend family in tracked topology and prints rollout order | Supported change tool |
| `topology-ssh.sh` | Runs one command or opens an interactive shell on a tracked host | Supported |
| `topology-ssh-script.sh` | Sends a multiline stdin script to a tracked host without nested quoting | Supported; distinct from `topology-ssh.sh` |

## Focused Runtime Recovery

These commands do not change topology and should not replace a deployment.

| Script | Role |
|---|---|
| `restart-gateway.sh` | Rebuilds/recreates Gateway with canonical runtime mounts; `--no-build` reuses the image |
| `restart-lifecycle-manager.sh` | Recreates Lifecycle Manager, including managed Colima context handling |
| `restart-colima.sh` | Restarts and verifies the managed macOS Colima runtime |
| `restart-mlx.sh` | Restarts the host-native macOS MLX launchd service |
| `redeploy-tts-shims.sh` | Focused rebuild/restart of Pocket/Lux/Qwen TTS shims; never touches Gateway |
| `switch-mlx-huge-model.sh` | Guarded manual replacement of the sole resident Huge MLX model |

## Verification And Diagnostics

| Script | Role |
|---|---|
| `verify-gateway.sh` | Runs the comprehensive verifier inside the running Gateway container; independent of upstream placement |
| `diagnose-gateway.sh` | Checks Gateway container state, HTTP contracts, and configured vLLM/MLX endpoints |
| `diagnose-telegram-bot.sh` | Checks Telegram configuration, container state, logs, and Gateway connectivity |
| `smoke-test-gateway.sh` | Exercises public Gateway health/models/embeddings/responses contracts |
| `smoke-vllm-tools.sh` | Exercises vLLM tool calling through the configured route |
| `vllm-tool-profile.py` | Lists/renders reusable vLLM tool profiles and rejects serving/Gateway env drift |
| `smoke-test-tts.sh` | Exercises TTS routing |
| `smoke-test-video.sh` | Exercises LTX or HunyuanVideo directly through the generation shim |
| `check-essential-containers.sh` | Waits for required control-plane containers and prints failure evidence |
| `check-etcd-health.sh` | Checks etcd endpoint/member health |

## Models And Cache

| Script | Role |
|---|---|
| `prewarm-vllm.sh` | Checks and warms strong, fast, and embeddings vLLM lanes |
| `prewarm-mlx.sh` | Checks and warms explicitly selected MLX models; supports alias discovery |
| `purge-mlx-model-cache.sh` | Guarded purge of one Hugging Face MLX repository cache |
| `sync-mlx-cache-status.sh` | Mirrors MLX cache/download status into Gateway-readable state |
| `run-vllm-openai.sh` | Container entrypoint that translates env policy into vLLM OpenAI-server arguments |

There is intentionally no generic `prewarm-models.sh`; operators must choose
the provider-specific behavior explicitly.

## Data, Registry, And Administration

| Script | Role |
|---|---|
| `backup-gateway-db.sh` / `restore-gateway-db.sh` | Consistent Gateway user DB backup and guarded restore |
| `backup-etcd.sh` / `restore-etcd.sh` | etcd snapshot backup and guarded restore |
| `install-gateway-db-backup-launchd.sh` / `gateway-db-backup-launch-agent.sh` | Install and run recurring Gateway DB backups on macOS |
| `install-etcd-backup-launchd.sh` / `etcd-backup-launch-agent.sh` | Install and run recurring etcd backups on macOS |
| `bootstrap-etcd-cluster.sh` | Coordinates first-time multi-member etcd bootstrap |
| `init-etcd-cluster.sh` | Writes one member's etcd cluster env settings |
| `register-service.sh` / `list-services.sh` | Manual etcd registration and inspection for external services |
| `reregister-all-services.sh` | Rewrites existing registrations with canonical metadata |
| `seed-tts-refs.sh` | Deduplicated import of shared TTS reference audio |
| `set-user-admin.sh` | Grants or revokes Gateway admin state in the host DB |

## Setup, Security, And Automation

| Script | Role |
|---|---|
| `install-host-deps.sh` | Interactive Docker/Compose/host dependency setup |
| `preflight-check.sh` | Dependency, configuration, permission, and selected-component checks |
| `import-env.sh` | Imports/synchronizes an operator env file |
| `materialize-sops-env.sh` / `sops-secrets.sh` | Materializes and manages encrypted host secret overlays |
| `allowlist-mlx-macos.sh` | Configures constrained macOS PF access to native MLX |
| `install-colima-launchd.sh` / `colima-launch-agent.sh` | Installs and runs managed Colima at boot |
| `install-backend-port-proxy-launchd.sh` / `backend-port-proxy.py` | Installs host port proxies used by topology routes |
| `install-coding-smoke-launchd.sh` / `coding-smoke-launch-agent.sh` | Installs and runs scheduled coding smoke tests |
| `run-coding-smoke-test.sh` / `coding-agent-smoke-test.py` | Executes the coding smoke workload |
| `generate-nginx-self-signed-cert.sh` | Creates local nginx development certificates |
| `lint-shell.sh` | Runs shell formatting/lint checks |

`_common.sh` and `_python.sh` are libraries, not operator entry points.

## Retired Migration And Compatibility Commands

The following unsafe or obsolete orchestration commands were removed:

| Retired script | Replacement |
|---|---|
| `ops-stack.sh` legacy implementation | Current guarded wrapper -> `deploy.sh --topology-host ...` |
| `backup-and-deploy-parallel.sh` | Data-specific backups plus topology deployment |
| `migrate-from-ai-infra.sh` | Data-specific restore tools plus topology deployment |
| `cutover-one-way.sh` | Data-specific recovery tools plus topology deployment |
| `cutover-tts-one-way.sh` | TTS data seeding plus topology deployment; routine recovery uses `redeploy-tts-shims.sh` |
| `stop-stack.sh` | Explicit service/container stop for the intended topology host |
| `restart-ai2-services.sh` | `deploy.sh --topology-host ai2 prod main` or a focused restart helper |
| `prewarm-models.sh` | `prewarm-vllm.sh` and/or `prewarm-mlx.sh` |

## Maintenance Rules

- Add placement only to `production.json` and `deploy.sh` component mapping.
- Do not add another script that assembles a production Compose stack.
- Shared env, Docker, Colima, bind-path, and confirmation logic belongs in
  `_common.sh`.
- Recovery scripts must name the service they affect and must not pull branches
  or change placement.
- Migration scripts must say `migration-only` in help text and documentation.
- Destructive data operations require an explicit target and confirmation or a
  dry-run mode.
