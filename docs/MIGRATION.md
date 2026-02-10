# Migration Guide: ai-infra to Nexus

This guide is now script-first. Use the interactive migration helper instead of running manual command sequences.

## Recommended Migration Path (Scripted)

1. Ensure migration scripts are executable:

```bash
chmod +x deploy/scripts/*.sh quickstart.sh
```

2. Run the host dependency installer if Docker/Docker Compose are not already present (interactive):

```bash
./deploy/scripts/install-host-deps.sh
```

3. Run the migration helper (interactive):

```bash
./deploy/scripts/migrate-from-ai-infra.sh
```

The migration helper can prompt for and/or automate:
- ai-infra backup creation (gateway data, Ollama model list, optional full Ollama archive)
- Nexus `.env` initialization and token generation
- `docker compose up -d`
- Restore of gateway/Ollama data and config backups
- Post-migration validation (`docker compose ps`, `/health`, optional `/v1/models`)
- Optional shutdown of legacy ai-infra services

## Non-Interactive Usage

For automated runs, provide all required paths and flags:

```bash
./deploy/scripts/migrate-from-ai-infra.sh \
  --ai-infra-dir "$HOME/ai-infra" \
  --backup-dir "$HOME/nexus-migration-backup" \
  --nexus-dir "$(pwd)" \
  --yes
```

Optional flags:
- `--skip-deploy`: backup + prep without `docker compose up -d`
- `--skip-restore`: skip restore steps and only perform backup/deploy/verify

## What Changed

The following previously manual sections are now handled by script workflows:
- Docker and Docker Compose installation
- Optional NVIDIA runtime installation
- Backup/restore command sequences
- Container copy/extract commands for migration artifacts
- Migration verification command sequence
- Optional legacy service shutdown

## Post-Migration Checks

After script completion, you can still run ad-hoc checks:

```bash
docker compose ps
docker compose logs --tail=100 gateway ollama
```

## Success Criteria

Migration is successful when:

- [ ] All services healthy (`docker compose ps`)
- [ ] Gateway responds to health checks
- [ ] Can list models via API
- [ ] Chat completions work
- [ ] Image generation works (if enabled)
- [ ] Historical data accessible
- [ ] Performance is acceptable
- [ ] Monitoring is functional
- [ ] Backups are working
- [ ] Old services can be safely stopped
