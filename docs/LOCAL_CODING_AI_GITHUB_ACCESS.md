# Local Coding AI GitHub Access Plan

This plan gives a local coding agent its own GitHub access to work on Nexus
without reusing a human operator's personal GitHub session.

## Goal

Allow a local AI coding worker to:

- read issues, pull requests, and repository files
- create branches and commits
- push draft PRs for human review
- inspect CI status and logs
- respond to review comments when assigned

The worker should not be able to push directly to protected branches or access
production secrets by default.

## Recommended Credential Model

Use a GitHub App installed only on the Nexus repository.

Recommended app permissions:

- Metadata: read
- Contents: read and write
- Pull requests: read and write
- Issues: read and write
- Checks: read
- Actions: read

Keep repository administration, secrets, environments, and organization-wide
permissions disabled unless a specific workflow requires them.

The app private key should live outside the repository in the local secret
store. A small local token helper can mint short-lived installation tokens for
the agent process. Prefer this over a long-lived personal access token.

## Simpler First Version

If a GitHub App is too much for the first iteration, create a dedicated machine
user such as `nexus-ai-bot` and issue a fine-grained personal access token
scoped only to `gevanoff/nexus`.

Minimum token permissions:

- Contents: read and write
- Pull requests: read and write
- Issues: read and write
- Actions: read
- Metadata: read

Store the token using `gh auth login --with-token` under a dedicated OS user,
WSL account, or container. Do not store the token in `.env`, Compose files,
repo docs, or agent prompts.

## Local Isolation

Run the coding AI in a separate workspace from the operator checkout:

- checkout root: `C:\Users\paper\Code\nexus-ai-worktrees` or a dedicated WSL path
- git identity: `Nexus AI Bot <nexus-ai-bot@users.noreply.github.com>`
- branch prefix: `ai/<ticket-or-task-slug>`
- PR mode: draft by default

The operator checkout can remain authenticated as the human account. The agent
checkout should use only the bot or GitHub App credential.

## Workflow Contract

1. Human creates or assigns an issue/task.
2. Agent creates a fresh branch from the target base branch.
3. Agent makes scoped changes and runs local checks.
4. Agent commits with a clear message and pushes the branch.
5. Agent opens a draft PR.
6. GitHub Actions runs.
7. Agent may inspect CI logs and push fixes.
8. Human reviews and merges.

The agent should never force-push shared branches or push directly to `main`.

## Repository Guardrails

Add or confirm these repository settings:

- protect `main`
- require PR review before merge
- require passing CI on protected branches
- disallow direct pushes to protected branches
- require linear history if desired
- optionally add CODEOWNERS for high-risk areas

Useful labels:

- `ai-ready`
- `ai-working`
- `ai-needs-human`
- `ai-produced`

## Nexus-Specific Boundaries

The coding AI can use GitHub and local tests freely, but production-impacting
operations should remain controlled:

- no automatic deploys from agent PRs
- no direct reads of private SSH keys or password files
- no printing of host IPs, tokens, or local secrets
- Nexus host access should go through existing deploy scripts or an explicit
  operator-approved runbook

## Open Decisions

- GitHub App versus machine user for the first implementation
- whether the agent runs as a Windows process, WSL process, or container
- which issues/labels should trigger autonomous work
- whether PR creation should be fully automatic or operator-approved
- whether the agent should be allowed to run GitHub Actions workflow dispatches

