# Host Env Files (deploy/env)

This folder is for **host-managed** environment files used by the deployment scripts.

## Why this exists

- Keep secrets **out of git** (this repo ignores `deploy/env/.env.dev` and `deploy/env/.env.prod`).
- Allow different configuration per host/environment.

## How to create env files

Start from the repo template:

- Source template: `../../.env.example`

Then create one of:

- `deploy/env/.env.dev` (for `deploy/scripts/deploy.sh dev ...`)
- `deploy/env/.env.prod` (for `deploy/scripts/deploy.sh prod ...`)

## Auto-create behavior

If you run `deploy/scripts/deploy.sh <dev|prod> <branch>` and it selects `deploy/env/.env.<environment>` (because you did not pass `--env-file` and there is no repo-root `.env`), it will create the file automatically from the repo-root `.env.example`.

This logic is implemented in `deploy/scripts/_common.sh` (`ns_ensure_env_file`).

You can also bypass these defaults and use any env file path with:

- `deploy/scripts/deploy.sh --env-file /path/to/env <dev|prod> <branch>`

## Permissions

On Linux/macOS, restrict env files to `600`.
