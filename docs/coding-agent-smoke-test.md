# Nexus Coding Agent Smoke Test

This smoke test exercises the live Nexus Coding framework through the
`/v1/coding/*` API. It is intended for regular unattended runs in production-like
environments.

## What It Verifies

The test creates a real Coding workspace from the configured Nexus repository,
starts the coding agent, and asks it to fix a small fixture in
`fixtures/coding-smoke-project`.

The task is intentionally narrow:

- Fix `fixtures/coding-smoke-project/math_tools.py`.
- Do not edit `fixtures/coding-smoke-project/verify_behavior.py`.
- Run `python -m unittest discover -s fixtures/coding-smoke-project -p "verify_*.py"`.
- Run `git diff --check`.
- Finish through the normal `coding_finish` tool.

The harness independently validates the final workspace through the Coding API.
It fails if the agent does not complete, validation fails, `git diff --check`
fails, the expected implementation file was not changed, or any file outside the
allowed fixture implementation path was changed.

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
  --start-interval 3600
```

The installer writes launcher state, logs, reports, lock files, and the plist
under `/ai-data/var/lib/nexus-smoke`. It also sets
`NEXUS_CODING_SMOKE_OUTPUT_DIR` in the Nexus env file so Gateway can mount and
display the same report directory.

Use `--weekly-models` for huge models that are not normally resident. The
launcher only runs that list during its weekly idle window, controlled by
`NEXUS_CODING_SMOKE_WEEKLY_DAY`, `NEXUS_CODING_SMOKE_IDLE_START_HOUR`, and
`NEXUS_CODING_SMOKE_IDLE_END_HOUR` in
`/ai-data/var/lib/nexus-smoke/coding/coding-smoke.env`.

Hourly is useful for active Coding hardening; daily is enough for basic
regression monitoring once the path is stable.

## Resources Health Panel

Gateway exposes summarized reports through:

- `GET /ui/api/coding/smoke-status`
- `GET /v1/coding/smoke-status`

The Resources UI shows the latest run, the requested model, resolved backend and
upstream model, duration, and a grouped metrics table by smoke profile and model.
The current profile is `fixture_median`; future profiles should use increasing
`profile_id`/`complexity` values so simple, medium, and larger coding tasks are
auditable separately.

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
