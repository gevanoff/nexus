# Tracked Secret Sources (`deploy/secrets`)

This directory holds the **encrypted, versioned source of truth** for host-specific
and environment-specific secret overlays.

## Layout

- `deploy/secrets/prod/common.env.sops`
- `deploy/secrets/prod/ai1.env.sops`
- `deploy/secrets/prod/ai2.env.sops`
- `deploy/secrets/prod/ada2.env.sops`
- `deploy/secrets/dev/default.env.sops`

Use `common.env.sops` for values shared by every host in an environment.
Use `<host>.env.sops` for tracked topology hosts.
Use `default.env.sops` only for non-topology single-host deploys.

## Control-node model

Keep the **age private key on the control node**, not in the repo.

Typical key path:

- `~/.config/sops/age/keys.txt`

Typical control-node bootstrap:

```bash
./deploy/scripts/sops-secrets.sh keygen
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
```

## Common workflows

Import an existing plaintext local overlay into a tracked encrypted file:

```bash
./deploy/scripts/sops-secrets.sh import-dotenv --input deploy/env/.env.prod.ai2.local --environment prod --host ai2
```

Edit an encrypted secret file in place:

```bash
./deploy/scripts/sops-secrets.sh edit --environment prod --host ai2
```

Decrypt for inspection:

```bash
./deploy/scripts/sops-secrets.sh decrypt --environment prod --host ai2
```

Materialize generated overlays from the encrypted secret files:

```bash
./deploy/scripts/sops-secrets.sh materialize --environment prod --topology-host ai2
```

## Deploy behavior

`deploy/scripts/deploy.sh` materializes:

1. `deploy/secrets/<environment>/common.env.sops`
2. `deploy/secrets/<environment>/<host>.env.sops` or `default.env.sops`

into generated git-ignored overlays next to the selected env file:

- `<env-file>.sops.common.local`
- `<env-file>.sops.local`

Those generated overlays are merged before the plain `.local` overlay.

## Git policy

- Track only `*.env.sops` files here.
- Do not commit plaintext `*.env` files.
- Do not store private keys in this repository.
