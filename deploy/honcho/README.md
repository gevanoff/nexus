# Honcho memory service on copyfail

`copyfail` is the preferred Honcho host for Nexus. It has enough memory and disk
for Honcho's API, one deriver, PostgreSQL/pgvector, and Redis, while all LLM and
embedding inference stays on the Nexus Gateway. Do not place model weights or a
model server on this host.

The Nexus compose file intentionally differs from Honcho's development defaults:

- Honcho v3.0.12 is pinned instead of building a moving branch.
- API authentication is mandatory before binding beyond loopback.
- PostgreSQL uses a password and is not published on the host.
- Redis is not published on the host.
- Text generation uses the tool-capable Nexus `default` alias.
- Embeddings use the Nexus `embeddings` alias at its verified 384 dimensions.

## Prepare the upstream source

On `copyfail`, clone the pinned upstream source beside Nexus:

```bash
git clone --branch v3.0.12 --depth 1 https://github.com/plastic-labs/honcho.git /home/ai/ai/honcho
```

Do not silently advance this checkout. Review Honcho release notes and database
migrations before changing `HONCHO_UPSTREAM_REF`.

## Configure secrets

```bash
cp deploy/honcho/honcho.env.example deploy/env/.env.prod.honcho
chmod 600 deploy/env/.env.prod.honcho
```

Populate the following without committing the file:

- `NEXUS_HOST_AI2_IP`: private address used by containers to reach `ai2`.
- `HONCHO_DB_PASSWORD`: random hex password for the private database.
- `AUTH_JWT_SECRET`: independent random Honcho signing secret.
- `LLM_OPENAI_API_KEY`: preferably a dedicated, restricted Nexus Gateway token.
- `HONCHO_BIND_ADDRESS`: copyfail's private Nexus address after auth is enabled;
  leave it at `127.0.0.1` while bootstrapping.

## Start and verify

Honcho migrations create 1536-dimensional pgvector columns by default. Nexus
embeddings are 384-dimensional, so configure the empty schema before starting
the API and deriver for the first time:

```bash
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml up -d honcho-database honcho-redis
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/alembic honcho upgrade head
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/python honcho \
  scripts/configure_embeddings.py --yes
```

The configuration helper refuses to alter populated embedding tables.

```bash
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml up -d --build
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml ps
curl http://127.0.0.1:8000/health
```

Generate a Honcho admin or workspace-scoped JWT using the pinned upstream helper
after the containers are healthy. Give chatbot integrations workspace-scoped
tokens rather than the admin token.

## Chatbot integration boundary

The server deployment is shared, but each chatbot identity should have its own
Honcho workspace/peer mapping. Before wiring the bots, decide:

- whether private chats isolate sessions per Telegram chat or per linked Nexus user;
- which group chats may contribute to long-term memory;
- whether host personas can read one another's memories;
- retention/deletion policy and who can export a user's memory.

Until that policy is explicit, do not automatically upload Telegram history.

The selected identity, sharing, export, and approved retention rules are
recorded in [`MEMORY_POLICY.md`](MEMORY_POLICY.md) and enforced by the Gateway's
Honcho memory registry.

## Enable Gateway, Chat UI, and Telegram ingestion

Honcho itself stays authenticated. Generate a workspace-scoped token (an admin
token is intentionally unnecessary) and add it to the private Gateway `.env` on
`ai2` without committing or printing it:

```bash
docker compose --env-file deploy/env/.env.prod.honcho \
  -f docker-compose.honcho.yml exec honcho \
  /app/.venv/bin/python scripts/generate_jwt.py \
  --workspace nexus --print-only
```

Honcho v3.0.12's helper currently writes `--expires` as an ISO string while its
JWT decoder requires the standard numeric claim, so an expiring token generated
by that pinned release is rejected as invalid. Until Honcho fixes the mismatch,
use the non-expiring workspace token above, store it only in the private
environment, and rotate it operationally every 90 days.

Configure these private Gateway deployment values:

```dotenv
HONCHO_MEMORY_ENABLED=true
HONCHO_BASE_URL=http://<copyfail-private-address>:8000
HONCHO_API_PREFIX=/v3
HONCHO_WORKSPACE_ID=nexus
HONCHO_WORKSPACE_TOKEN=<workspace-scoped-jwt>
HONCHO_PRIVATE_RAW_RETENTION_DAYS=180
HONCHO_GROUP_RAW_RETENTION_DAYS=90
HONCHO_EXPORT_RETENTION_DAYS=7
HONCHO_AUDIT_RETENTION_DAYS=365
```

When these Gateway settings are enabled, authenticated Nexus Chat UI sessions
retrieve Honcho context before inference and store each completed turn after the
assistant response. Anonymous UI sessions are deliberately excluded because
they have no durable Nexus owner identity. Model aliases that declare a `soul`
also prepend the corresponding `souls/<name>/SOUL.md`; raw UI memory remains
partitioned by Nexus user, UI conversation, and soul identity.

After the Gateway reports `/v1/telegram/memory/status` as enabled, set
`TELEGRAM_MEMORY_ENABLED=true` in each bot host's private `.env` and rebuild the
bot. Memory failures are non-fatal to chat replies, but they are logged and do
not silently claim that a turn was stored.

The Gateway stores its enforcement registry at
`/var/lib/gateway/data/honcho_memory.sqlite`, writes mode-`0600` exports beneath
`/var/lib/gateway/data/honcho_exports`, runs retention hourly, and preserves
long-term conclusions before expiring raw per-turn sessions. Rotate the
workspace JWT every 90 days while the pinned upstream expiry bug remains.

## Backup

Back up the `nexus-honcho_honcho-pgdata` volume with `pg_dump`; Redis is a cache
and is not the source of truth. Test a restore before enabling chatbot ingestion.
