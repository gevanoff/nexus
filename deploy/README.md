# Deployment Manifests

This directory provides per-service manifests for Docker Compose and containerd (via nerdctl).

## Docker Compose

Use the deployment wrappers instead of manual compose command sequences. From the repository root:

```bash
./deploy/scripts/deploy.sh dev dev
```

For remote hosts:

```bash
./deploy/scripts/remote-deploy.sh dev dev user@dev-host
```

## containerd (nerdctl)

Containerd manifests remain available in `deploy/containerd/`, but operational install/deploy guidance is script-first via `deploy/scripts/*.sh`.

## Setup and Deployment Scripts

Make sure helper scripts are executable before first use:

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
```

Script entrypoints (all invoked from repo root):

- `./quickstart.sh`: interactive local bootstrap (preflight + `.env` + startup)
- `./deploy/scripts/preflight-check.sh`: host validation for required tools/files/permissions
- `./deploy/scripts/deploy.sh [--component NAME|--components LIST] <dev|prod> <branch>`: deploy selected components on a host
- `./deploy/scripts/remote-deploy.sh [--component NAME|--components LIST] [--topology-host NAME] [--repo-dir PATH] <dev|prod> <branch> [user@host]`: deploy selected components over SSH
- `./deploy/scripts/ansible-topology.sh <inventory|bootstrap|deploy|site> [host|all] [-- extra ansible args]`: short wrapper around the topology-backed Ansible control layer
- `./deploy/scripts/topology-ssh.sh [--print-target] <ai1|ai2|ada2> [command...]`: resolve a tracked host profile to SSH and optionally run a remote command
- `./deploy/scripts/render-topology-env.sh --topology-host <host>`: materialize a host env file from the tracked topology manifest
- `./deploy/scripts/reassign-topology-family.sh --family <name> --from <host> --to <host> [--write]`: move a tracked backend family between topology hosts
- `./deploy/scripts/materialize-sops-env.sh --environment <dev|prod> [--topology-host <host>]`: materialize tracked SOPS secret files into generated `*.sops.local` overlays
- `./deploy/scripts/sops-secrets.sh <keygen|import-dotenv|edit|decrypt|materialize> ...`: manage SOPS+age secret files under `deploy/secrets/`
- `./deploy/scripts/seed-tts-refs.sh --source <path>`: seed shared `./.runtime/tts_refs` with deduped reference audio
- `./deploy/scripts/register-service.sh [--backend-class CLASS] <name> <base-url> <etcd-url>`: register backend in etcd
- `./deploy/scripts/list-services.sh <etcd-url>`: inspect registered services
- `./deploy/scripts/smoke-test-video.sh`: run a SkyReels video smoke test (direct backend by default, or the gateway UI path when UI credentials are provided)
- `./deploy/scripts/backup-and-deploy-parallel.sh`: backup legacy host data (best-effort) and deploy Nexus on parallel ports

Example: deploy only the images component to a GPU host:

```bash
./deploy/scripts/deploy.sh --components images prod main
```

Example: deploy an NVIDIA image stack on Linux:

```bash
./deploy/scripts/deploy.sh --components invokeai,images,sdxl-turbo prod main
```

Example: deploy the streaming stack on `ai1`:

```bash
./deploy/scripts/deploy.sh --components mediamtx prod main
```

Example: deploy only the vLLM fast + embeddings lanes on `ai1`:

```bash
./deploy/scripts/deploy.sh --components vllm-fast,vllm-embeddings prod main
```

Example: deploy only the vLLM strong lane on `ada2`:

```bash
./deploy/scripts/deploy.sh --components vllm-strong prod main
```

Example: deploy the explicit `ai1` topology profile:

```bash
./deploy/scripts/deploy.sh --topology-host ai1 prod main
```

Example: deploy the explicit `ai1` topology profile over SSH without repeating the host target:

```bash
./deploy/scripts/remote-deploy.sh --topology-host ai1 prod main
./deploy/scripts/ansible-topology.sh deploy ai1
./deploy/scripts/topology-ssh.sh ai1 docker ps
```

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

Example: deploy Linux/NVIDIA Ollama explicitly with the GPU override:

```bash
./deploy/scripts/deploy.sh --component ollama-linux-nvidia prod main
```


## Recommended Sequence

Local (single host):

1. `./quickstart.sh` (recommended)

Manual local alternative:

1. `./deploy/scripts/preflight-check.sh`
2. `cp .env.example .env` (edit as needed)
3. `docker compose up -d`

Parallel (side-by-side with an existing gateway/ai-infra deployment):

1. `./deploy/scripts/backup-and-deploy-parallel.sh` (recommended for first parallel cutover)

Remote host deploy:

1. Standardize the remote host layout:
	 - Deploy user: `ai`
	 - Repo location:
		 - macOS: `/Users/ai/ai/nexus`
		 - Linux: `/home/ai/ai/nexus`
	 - Ownership:
		 - macOS: `ai:staff`
		 - Linux: `ai:ai`
2. Clone this repo to the platform-specific repo path on the remote host (as the `ai` user)
3. Run `./deploy/scripts/remote-deploy.sh <dev|prod> <branch> <ai@host>` from your local machine
4. For tracked cluster hosts, prefer `./deploy/scripts/remote-deploy.sh --topology-host <ai1|ai2|ada2> <dev|prod> <branch>` so SSH target and repo path come from `deploy/topology/production.json`

## Windows development note

Nexus is deployed/operated from macOS/Linux hosts. If you develop on Windows, run all `deploy/scripts/*.sh` scripts from within WSL (Ubuntu) rather than PowerShell.

## Notes

- These manifests assume a shared `nexus` network for multi-host deployments.
- `deploy/topology/production.json` is the desired-state source of truth for host placement in the current `ai1`/`ai2`/`ada2` cluster.
- etcd is the live runtime registry, not the deployment plan. Service registrars should publish healthy endpoints into etcd after the topology has been deployed.
- Keep `DEFAULT_BACKEND` and `EMBEDDINGS_BACKEND` aligned with the intended host role; on `ai2`, prefer `local_mlx`.
- `vllm` remains the monolithic three-lane profile; use `vllm-strong`, `vllm-fast`, and `vllm-embeddings` when different hosts should own different inference lanes.
- Persistence uses host bind mounts under `./.runtime/` (including gateway RO config at `./.runtime/gateway/config`).
- The UI is intentionally separated from the gateway for production deployments; keep it as a standalone container when it is implemented.
- For branch-based deploys, see `./deploy/scripts/deploy.sh` and `./deploy/scripts/remote-deploy.sh` (invoked from repo root).
- For etcd convenience, use `./deploy/scripts/register-service.sh` and `./deploy/scripts/list-services.sh` (invoked from repo root).
