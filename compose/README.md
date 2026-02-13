# Nexus Compose files

Policy: **one Docker Compose file per component**.

Current layout (preferred): per-component compose files live in the `nexus/` root so all bind-mount paths remain unchanged.

- `docker-compose.gateway.yml`
- `docker-compose.ollama.yml`
- `docker-compose.etcd.yml`
- `docker-compose.images.yml`
- `docker-compose.tts.yml`

Dev overrides (layer on top of the corresponding base file):

- `docker-compose.gateway.dev.yml`
- `docker-compose.ollama.dev.yml`
- `docker-compose.etcd.dev.yml`
- `docker-compose.images.dev.yml`
- `docker-compose.tts.dev.yml`

This `compose/` directory is kept only for historical context; the active compose entrypoints are the root-level files listed above.
