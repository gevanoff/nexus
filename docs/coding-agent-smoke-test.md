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

Run the wrapper from launchd, cron, or any existing Nexus supervisor. For ai2,
keep logs and reports under `/ai-data`, for example:

```bash
NEXUS_CODING_SMOKE_OUTPUT_DIR=/ai-data/var/lib/nexus-smoke/coding \
  /ai-data/var/lib/nexus/deploy/scripts/run-coding-smoke-test.sh
```

Use a cadence that matches model availability. Hourly is useful for active
development; daily is enough for basic regression monitoring.

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
