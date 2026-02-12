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

You can also bypass these defaults and use any env file path with:

- `deploy/scripts/deploy.sh --env-file /path/to/env <dev|prod> <branch>`

## Permissions

On Linux/macOS, restrict env files to `600`.
