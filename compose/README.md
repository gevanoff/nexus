# Nexus Compose files

Policy: **one Docker Compose file per component**.

Current layout (preferred): per-component compose files live in the `nexus/` root so all bind-mount paths remain unchanged.

- `docker-compose.gateway.yml`
- `docker-compose.vllm.yml`
- `docker-compose.etcd.yml`
- `docker-compose.images.yml`
- `docker-compose.tts.yml`
- `docker-compose.telegram-bot.yml`

This `compose/` directory is kept only for historical context; the active compose entrypoints are the root-level files listed above.

Its file-local `../.runtime` defaults are compatibility leftovers for repo-local experimentation, not the deployment standard for managed hosts.
