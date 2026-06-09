# Nexus Coding Agent Smoke Test

This smoke test exercises the live Nexus Coding framework through the
`/v1/coding/*` API. It is intended for regular unattended runs in production-like
environments.

## What It Verifies

The test creates a real Coding workspace from the configured Nexus repository,
starts the coding agent, and asks it to fix one of the checked-in smoke
fixtures. The default scheduled suite runs these escalating profiles:

| Profile | Complexity | Expected edits |
| --- | --- | --- |
| `fixture_median` | simple | One-line median repair in `fixtures/coding-smoke-project/math_tools.py`. |
| `fixture_inventory` | medium | Single-file SKU normalization and inventory aggregation in `fixtures/coding-smoke-inventory/inventory_tools.py`. |
| `fixture_route_flags` | moderate | Two-file feature flag parsing and route normalization in `fixtures/coding-smoke-routing/feature_flags.py` and `router.py`. |

Each profile tells the agent to run that fixture's unittest discovery command,
run `git diff --check`, inspect its diff, and finish through the normal
`coding_finish` tool.

The harness independently validates the final workspace through the Coding API.
It fails if the agent does not complete, validation fails, `git diff --check`
fails, the expected implementation file was not changed, or any file outside the
allowed fixture implementation path was changed. Passing smoke workspaces are
archived automatically after validation, and the JSON report records the archive
result; failed workspaces stay active for debugging.

## Run Manually

From the Nexus repo on a host that can reach Gateway:

```bash
./deploy/scripts/run-coding-smoke-test.sh
```

Useful options:

```bash
./deploy/scripts/run-coding-smoke-test.sh \
  --base-url http://127.0.0.1:8800 \
  --env-file /ai-data/var/lib/nexus/.env \
  --model coder \
  --profile-id fixture_inventory \
  --timeout-sec 1200
```

On ai2, reports default to:

```text
/ai-data/var/lib/nexus-smoke/coding
```

The script also prints the JSON report to stdout.

## Scheduling

On ai2, install the recurring launchd job without placing job-owned files on the
internal disk:

```bash
cd /ai-data/var/lib/nexus
./deploy/scripts/install-coding-smoke-launchd.sh \
  --repo-dir /ai-data/var/lib/nexus \
  --env-file /ai-data/var/lib/nexus/.env \
  --output-dir /ai-data/var/lib/nexus-smoke/coding \
  --models coder \
  --profiles fixture_median,fixture_inventory,fixture_route_flags \
  --start-interval 3600
```

The installer writes launcher assets and the plist under
`/ai-data/launchd/nexus-smoke`, while runtime env, logs, reports, and lock files
live under `/ai-data/var/lib/nexus-smoke`. It also sets
`NEXUS_CODING_SMOKE_OUTPUT_DIR` in the Nexus env file so Gateway can mount and
display the same report directory.

Use `--weekly-models` for huge models that are not normally resident. The
launcher only runs that list during its weekly idle window and uses
`--weekly-profiles` when supplied. The idle window is controlled by
`NEXUS_CODING_SMOKE_WEEKLY_DAY`, `NEXUS_CODING_SMOKE_IDLE_START_HOUR`, and
`NEXUS_CODING_SMOKE_IDLE_END_HOUR` in
`/ai-data/var/lib/nexus-smoke/coding/coding-smoke.env`.

For example, to test the active coder model hourly and non-active huge models
weekly during the idle window:

```bash
./deploy/scripts/install-coding-smoke-launchd.sh \
  --repo-dir /ai-data/var/lib/nexus \
  --env-file /ai-data/var/lib/nexus/.env \
  --output-dir /ai-data/var/lib/nexus-smoke/coding \
  --models coder,strong \
  --profiles fixture_median,fixture_inventory,fixture_route_flags \
  --weekly-models mlx-community/DeepSeek-R1-0528-4bit,mlx-community/GLM-4.5-4bit \
  --weekly-profiles fixture_median,fixture_inventory
```

Hourly is useful for active Coding hardening; daily is enough for basic
regression monitoring once the path is stable.

## Resources Health Panel

Gateway exposes summarized reports through:

- `GET /ui/api/coding/smoke-status`
- `GET /v1/coding/smoke-status`

The Resources UI shows the latest run, the requested model, resolved backend and
upstream model, duration, and a grouped metrics table by smoke profile and model.
Each row includes the profile label, complexity, model/backend identity, pass
rate, average duration, and latest duration so regressions are visible by model
size and task complexity.

## Monitoring And Intervention APIs

The smoke harness uses these bearer-authenticated Coding APIs:

- `GET /v1/coding/monitor`
- `GET /v1/coding/tasks/{task_id}/inspect`
- `POST /v1/coding/tasks/{task_id}/intervene`

`/inspect` returns attention flags, safe actions, and a recommended action for a
workspace. `/intervene` supports `guidance`, `guide_and_resume`, `resume`,
`pause`, and `stop`. This gives operator agents a bounded control surface for
recovering stalled or failed coding workspaces without scraping local task files.

The smoke harness can auto-intervene a limited number of times when the monitor
reports a safe recommended action. It records each intervention in the JSON
report.
