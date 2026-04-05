# Host Env Files (deploy/env)

This folder is for **host-managed** environment files used by the deployment scripts.

## Why this exists

- Keep secrets **out of git** (this repo ignores `deploy/env/.env.*`).
- Allow different configuration per host/environment.

## How to create env files

Start from the repo template:

- Source template: `../../.env.example`

Then create one of:

- `deploy/env/.env.dev` (for `deploy/scripts/deploy.sh dev ...`)
- `deploy/env/.env.prod` (for `deploy/scripts/deploy.sh prod ...`)
- `deploy/env/.env.prod.ai1`, `deploy/env/.env.prod.ai2`, `deploy/env/.env.prod.ada2` when using topology-driven multi-host deploys

Optional untracked overlays:

- `deploy/env/.env.dev.local`
- `deploy/env/.env.prod.local`
- `deploy/env/.env.prod.ai1.local`, `deploy/env/.env.prod.ai2.local`, `deploy/env/.env.prod.ada2.local`

These `.local` files are git-ignored and are applied after the tracked template/topology env is rendered. Use them for host-local secrets, allowlists, reference-file paths, and other values that should not live in the repo.

## Auto-create behavior

If you run `deploy/scripts/deploy.sh <dev|prod> <branch>` and it selects `deploy/env/.env.<environment>` (because you did not pass `--env-file` and there is no repo-root `.env`), it will create the file automatically from the repo-root `.env.example`.

If you run `deploy/scripts/deploy.sh --topology-host <host> <dev|prod> <branch>`, it materializes `deploy/env/.env.<environment>.<host>` from the tracked topology manifest before deploy.

If a sibling `.local` overlay exists, the deploy scripts merge it into the selected env file before preflight and compose startup.

This logic is implemented in `deploy/scripts/_common.sh` (`ns_ensure_env_file`).

You can also bypass these defaults and use any env file path with:

- `deploy/scripts/deploy.sh --env-file /path/to/env <dev|prod> <branch>`

## Permissions

On Linux/macOS, restrict env files to `600`.
