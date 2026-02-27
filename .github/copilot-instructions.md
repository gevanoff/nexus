# Copilot instructions (nexus)

## Repo role and relationships
- `nexus/` is the primary operations repo and the current source of truth for compose/deploy/runtime workflows.
- Treat Nexus as the operational aggregation point for:
  - Gateway behavior and contracts (implemented in `gateway/`).
  - Infra/deploy assumptions historically captured in `ai-infra/`.
- If a change affects runtime operations, prefer implementing/updating it in `nexus/` docs/scripts first.

## Scope boundaries
- The development host is never used as a deployment host.
- Development-host helper scripts may exist, but are not the default production/test deploy path.
- Keep changes minimal and rooted in real deploy/runtime needs.

## Commit and distribution policy
- If code updates require user changes on test/production hosts, commit and push to `origin`.
- Do not stop at recommendations when practical code/docs updates can be completed safely.

## Cross-platform script guidance (Linux + macOS)
- For scripts intended to run on Linux and macOS, write with both in mind.
- Detect OS (`uname`/platform checks) and auto-select compatible behavior.
- Avoid GNU-only assumptions unless guarded by OS checks; include BSD/macOS-safe alternatives.
- Prefer portable shell patterns and fail clearly when prerequisites are missing.

## Reuse and consistency rules
- Before modifying code, search for existing similar implementations in-repo and follow established patterns.
- When fixing one repeated pattern, apply the same fix across equivalent call sites where appropriate.
- Add explicit maintenance markers when needed to link related logic blocks, using a structured tag like `SYNC-CHECK(<topic>)`.
- When shared logic is repeated, prefer extracting helpers/libraries to reduce drift.

## Security and hardening
- After code changes, perform a quick security pass for obvious risks (unsafe shell execution, path traversal, secret leakage, missing input validation, insecure defaults).
- Apply basic hardening fixes within scope before handing off.

## Response/hand-off expectations
- Provide next practical steps after updates.
- If commands are needed, provide one-liners where possible.
- If one line is not possible, provide one command per code block.

## Operational references
- Primary docs/scripts: `nexus/deploy/`, `nexus/docs/`, top-level compose files.
- Gateway implementation reference: `gateway/app/` and `gateway/tests/`.
- Historical infra reference: `ai-infra/services/` (especially gateway runtime docs).
