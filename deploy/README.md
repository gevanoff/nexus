# Deployment Manifests

See [SCRIPTS.md](SCRIPTS.md) for the authoritative script map, command hierarchy,
retired entry points, and maintenance rules.

This directory provides per-service manifests for Docker Compose and containerd (via nerdctl).

## Docker Compose

Use the deployment wrappers instead of manual compose command sequences. From the repository root:

```bash
./deploy/scripts/deploy.sh prod main
```

For remote hosts:

```bash
./deploy/scripts/remote-deploy.sh prod main user@prod-host
```

## containerd (nerdctl)

Containerd manifests remain available in `deploy/containerd/`, but operational install/deploy guidance is script-first via `deploy/scripts/*.sh`.

Both the deploy compose manifests and the containerd manifests assume the selected host env file sets `NEXUS_RUNTIME_ROOT` explicitly.

## Setup and Deployment Scripts

Make sure helper scripts are executable before first use:

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
```

Script entrypoints (all invoked from repo root):

- `./quickstart.sh`: interactive local bootstrap (preflight + `.env` + startup)
- `./deploy/scripts/preflight-check.sh`: host validation for required tools/files/permissions
- `./deploy/scripts/deploy.sh [--component NAME|--components LIST] <prod> <branch>`: deploy selected components on a host
- `./deploy/scripts/ops-stack.sh --topology-host <host> [prod main]`: guarded convenience wrapper around `deploy.sh` with production defaults
- `./deploy/scripts/remote-deploy.sh [--component NAME|--components LIST] [--topology-host NAME] [--repo-dir PATH] <prod> <branch> [user@host]`: deploy selected components over SSH
- `./deploy/scripts/request-deploy.sh --host HOST --component NAME [--reason TEXT]`: preferred production entry point; submit and follow a serialized deployment through Deployment Control on `copyfail`
- `./deploy/scripts/ansible-topology.sh <inventory|bootstrap|deploy|site> [host|all] [-- extra ansible args]`: short wrapper around the topology-backed Ansible control layer
- `./deploy/scripts/topology-ssh.sh [--print-target] <stackrot|ai2|ada2|meltdown|copyfail> [command...]`: resolve a tracked host profile to SSH and optionally run a remote command
- `./deploy/scripts/render-topology-env.sh --topology-host <host>`: materialize a host env file from the tracked topology manifest
- `./deploy/scripts/reassign-topology-family.sh --family <name> --from <host> --to <host> [--write]`: move a tracked backend family between topology hosts
- `./deploy/scripts/materialize-sops-env.sh --environment <dev|prod> [--topology-host <host>]`: materialize tracked SOPS secret files into generated `*.sops.local` overlays
- `./deploy/scripts/sops-secrets.sh <keygen|import-dotenv|edit|decrypt|materialize> ...`: manage SOPS+age secret files under `deploy/secrets/`
- `./deploy/scripts/seed-tts-refs.sh --source <path>`: seed shared `${NEXUS_RUNTIME_ROOT}/tts_refs` with deduped reference audio
- `./deploy/scripts/backup-gateway-db.sh [--env-file PATH] [--output PATH] [--keep COUNT] [--ssh-target USER@HOST --ssh-dir PATH] [--rclone-remote DEST]`: take a consistent snapshot of `gateway/data/users.sqlite`, keep local retention, and optionally mirror the bundle to SSH or `rclone` destinations
- `./deploy/scripts/install-gateway-db-backup-launchd.sh [--start-interval SEC] [--ssh-target USER@HOST --ssh-dir PATH] [--rclone-remote DEST]`: install/reload a macOS launchd job for recurring gateway DB backups on hosts such as `ai2`
- `./deploy/scripts/backup-etcd.sh [--env-file PATH] [--container NAME] [--endpoints URLS] [--output PATH] [--keep COUNT] [--ssh-target USER@HOST --ssh-dir PATH] [--rclone-remote DEST]`: take an etcd snapshot, keep local retention, and optionally mirror the bundle to SSH or `rclone` destinations
- `./deploy/scripts/install-etcd-backup-launchd.sh [--start-interval SEC] [--ssh-target USER@HOST --ssh-dir PATH] [--rclone-remote DEST]`: install/reload a macOS launchd job for recurring etcd snapshot backups on hosts such as `ai2`
- `./deploy/scripts/restore-gateway-db.sh --snapshot PATH [--env-file PATH] [--force]`: restore a compressed `users.sqlite` backup into the canonical runtime path while preserving the current DB beside it
- `./deploy/scripts/register-service.sh [--backend-class CLASS] <name> <base-url> <etcd-url>`: register backend in etcd
- `./deploy/scripts/list-services.sh <etcd-url>`: inspect registered services
- `./deploy/scripts/smoke-test-video.sh`: run a SkyReels video smoke test (direct backend by default, or the gateway UI path when UI credentials are provided)

Example: deploy only the images component to a GPU host:

```bash
./deploy/scripts/request-deploy.sh --host ada2 --component images --reason "deploy image service"
```

Example: deploy an NVIDIA image stack on Linux:

```bash
./deploy/scripts/deploy.sh --components invokeai,images,sdxl-turbo prod main
```

Example: deploy the streaming stack on `stackrot`:

```bash
./deploy/scripts/deploy.sh --components mediamtx prod main
```

Example: deploy only the vLLM fast + embeddings lanes on `stackrot`:

```bash
./deploy/scripts/deploy.sh --components vllm-fast,vllm-embeddings prod main
```

Example: deploy only the vLLM strong lane on `ada2`:

```bash
./deploy/scripts/deploy.sh --components vllm-strong prod main
```

Example: deploy the explicit `stackrot` topology profile:

```bash
./deploy/scripts/deploy.sh --topology-host stackrot prod main
```

Example: deploy the explicit `stackrot` topology profile over SSH without repeating the host target:

```bash
./deploy/scripts/remote-deploy.sh --topology-host stackrot prod main
./deploy/scripts/ansible-topology.sh deploy stackrot
./deploy/scripts/topology-ssh.sh stackrot docker ps
```

Example: prepare `copyfail` as the lightweight infrastructure control host:

```bash
./deploy/scripts/ansible-topology.sh bootstrap copyfail
./deploy/scripts/topology-ssh.sh copyfail 'cd /home/ai/ai/nexus && git pull --ff-only'
```

After `copyfail` is bootstrapped, prefer running routine Ansible-driven deployments from that host so deploy state, logs, and control-node tooling converge in one place. `copyfail` is intentionally not a model-serving host.

Backend-family reassignment routine:

```bash
./deploy/scripts/reassign-topology-family.sh --family vllm --from ai2 --to ada2 --write
```

Recommended rollout order after changing topology:

1. Deploy the destination host first so the service family comes up on the new node.
2. Deploy any gateway host next so rendered env files pick up the new backend URLs.
3. Deploy the source host last so old components are removed.
4. Verify gateway health/smoke, run `./deploy/scripts/smoke-test-video.sh` when video backends changed, and re-register services if registry drift remains.

When moving `vllm`, also make sure the destination host has `HUGGING_FACE_HUB_TOKEN` when the tracked model family requires Hugging Face auth or higher rate limits.

Host-local secret overlays:

- For any selected env file, you can add a sibling `.local` file such as `deploy/env/.env.prod.ai2.local`.
- The deploy scripts merge that overlay after rendering the tracked env file and before preflight/compose.
- Keep tokens, IP allowlists, reference-audio paths, and other host-only values there instead of in `production.json`.

Tracked encrypted host secrets:

- Store versioned secret sources in `deploy/secrets/<environment>/common.env.sops` and `deploy/secrets/<environment>/<host>.env.sops`.
- The controller-side deploy wrappers materialize those files into generated `deploy/env/.env.*.sops*.local` overlays before syncing them to the target host.
- Manual `.local` overlays still work and override the generated SOPS overlays when both are present.

Recommended SOPS bootstrap on the control node:

```bash
./deploy/scripts/sops-secrets.sh keygen
./deploy/scripts/sops-secrets.sh import-dotenv --input deploy/env/.env.prod.ai2.local --environment prod --host ai2
./deploy/scripts/sops-secrets.sh edit --environment prod --host ai2
```

Example: deploy the strong vLLM lane explicitly:

```bash
./deploy/scripts/deploy.sh --component vllm-strong prod main
```

Gateway DB backup examples:

Keep 30 local snapshots under the canonical runtime root and mirror each bundle to `copyfail`:

```bash
./deploy/scripts/backup-gateway-db.sh --ssh-target ai@copyfail --ssh-dir /home/ai/backups/nexus/gateway-db/ai2
```

Mirror the same backup bundle to a private cloud destination that has already been configured in `rclone`:

```bash
./deploy/scripts/backup-gateway-db.sh --rclone-remote private:nexus/gateway-db/ai2
```

Install a recurring `ai2` launchd job that runs every six hours and mirrors backups to another cluster host:

```bash
sudo ./deploy/scripts/install-gateway-db-backup-launchd.sh --user ai --start-interval 21600 --ssh-target ai@copyfail --ssh-dir /home/ai/backups/nexus/gateway-db/ai2
```

Etcd backup examples:

Keep 30 local snapshots under the canonical runtime root and mirror each bundle to `copyfail`:

```bash
./deploy/scripts/backup-etcd.sh --ssh-target ai@copyfail --ssh-dir /home/ai/backups/nexus/etcd/ai2
```

Install a recurring `ai2` launchd job that runs every six hours and mirrors etcd snapshots to another cluster host:

```bash
sudo ./deploy/scripts/install-etcd-backup-launchd.sh --user ai --start-interval 21600 --ssh-target ai@copyfail --ssh-dir /home/ai/backups/nexus/etcd/ai2
```

The gateway DB backup script resolves the source database from `NEXUS_RUNTIME_ROOT` via the repo `.env`, so it follows the canonical host runtime path instead of accidentally backing up a repo-local `.runtime` override.

Deployment note: active deploy manifests should be treated as requiring `NEXUS_RUNTIME_ROOT` in the selected host env file. Do not rely on compose-file-relative `.runtime` fallbacks for deployed hosts.


## Recommended Sequence

Local (single host):

1. `./quickstart.sh` (recommended)

Manual local alternative:

1. `./deploy/scripts/preflight-check.sh`
2. `cp .env.example .env` (edit as needed)
3. `docker compose up -d`

Remote host deploy:

1. Standardize the remote host layout:
	 - Deploy user: `ai`
	 - Repo location:
		 - macOS: `/ai-data/var/lib/nexus`
		 - Linux: `/home/ai/ai/nexus`
	 - Ownership:
		 - macOS: `ai:staff`
		 - Linux: `ai:ai`
2. Clone this repo to the platform-specific repo path on the remote host (as the `ai` user)
3. Run `./deploy/scripts/remote-deploy.sh <prod> <branch> <ai@host>` from your local machine
4. For tracked cluster hosts, prefer `./deploy/scripts/remote-deploy.sh --topology-host <stackrot|ai2|ada2|meltdown|copyfail> <prod> <branch>` so SSH target and repo path come from `deploy/topology/production.json`

The remote wrapper fast-forwards the target checkout before it invokes that
checkout's preflight and deploy scripts. This lets Deployment Control roll out a
new component name without requiring a separate manual pull on every target.

## Windows development note

Nexus is deployed/operated from macOS/Linux hosts. If you develop on Windows, run all `deploy/scripts/*.sh` scripts from within WSL (Ubuntu) rather than PowerShell.

When you need to run multi-step commands on a tracked remote host, prefer a checked-in script or `./deploy/scripts/topology-ssh-script.sh <host> <<'EOF' ... EOF` over nested quoted one-liners. This avoids PowerShell, WSL, SSH, and remote-shell quoting interacting in unpredictable ways.

Do not `source` the tracked deploy env files directly. They are dotenv files, not guaranteed shell scripts. Use `--env-file`, `ns_env_get`, or the repo's env materialization/overlay helpers instead.

Recommended shell setup for contributors:

```bash
sudo apt-get install -y shellcheck shfmt
pipx install pre-commit
pre-commit install
./deploy/scripts/lint-shell.sh
```

`./deploy/scripts/lint-shell.sh` checks changed shell files by default. Use `./deploy/scripts/lint-shell.sh --all` only when you intentionally want to sweep the whole repo for shell formatting and lint drift.

## Notes

- These manifests assume a shared `nexus` network for multi-host deployments.
- `deploy/topology/production.json` is the desired-state source of truth for host placement in the current `stackrot`/`ai2`/`ada2`/`meltdown` cluster, the constrained native MLX lane on `migraine`, and the `copyfail` infrastructure-control host.
- etcd is the live runtime registry, not the deployment plan. Service registrars should publish healthy endpoints into etcd after the topology has been deployed.
- Keep `DEFAULT_BACKEND` and `EMBEDDINGS_BACKEND` aligned with the intended host role; on `ai2`, prefer `local_mlx`.
- `vllm` remains the monolithic three-lane profile; use `vllm-strong`, `vllm-fast`, `vllm-embeddings`, and the dedicated `vllm-meltdown` Cinder lane when different hosts should own different inference services.
- Persistence uses host bind mounts under `${NEXUS_RUNTIME_ROOT}/` (including gateway RO config at `${NEXUS_RUNTIME_ROOT}/gateway/config`). For deployed hosts, set `NEXUS_RUNTIME_ROOT` explicitly in the selected host env file.
- The UI is intentionally separated from the gateway for production deployments; keep it as a standalone container when it is implemented.
- For branch-based deploys, see `./deploy/scripts/deploy.sh` and `./deploy/scripts/remote-deploy.sh` (invoked from repo root).
- For etcd convenience, use `./deploy/scripts/register-service.sh` and `./deploy/scripts/list-services.sh` (invoked from repo root).
