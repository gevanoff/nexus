# Nexus Agent Instructions

These instructions apply to work inside this repository. Revalidate live service
state before acting on operational notes; Nexus hosts and model routes can change.

## Shell And Repo Work

- At the start of every Nexus task, read this `AGENTS.md` file before any repo
  inspection, host access, edits, tests, git operations, or deployment commands.
  If the user provides newer AGENTS-style instructions in chat, follow the newer
  instructions too and reconcile any conflict explicitly.
- Never use PowerShell for Nexus repo work, deployment work, or Nexus host
  operations. In Codex `exec_command` calls, explicitly set `shell: "bash"` and
  use POSIX shell syntax.
- Do all Nexus repo inspection, edits, greps, tests, and git operations from the
  local WSL/Linux checkout.
- From Windows Codex sessions, use this pattern:

  ```bash
  wsl.exe -d Ubuntu-20.04 -- bash -lc 'cd /mnt/c/Users/paper/Code/nexus && <command>'
  ```

- If the repo or required helper tools are not accessible through WSL, stop and
  report the missing prerequisite. Do not continue the same repo task through
  PowerShell.
- If an example or existing command is written in PowerShell syntax, translate it
  to Bash/WSL before running it.
- Prefer `rg` and `rg --files` for searches.
- Do not print, copy, or modify private SSH keys, password files, bearer tokens,
  or private `.env` values unless the user explicitly asks for that exact secret
  operation.

## Nexus Host Access

- Use the WSL SSH aliases for private Nexus hosts. For example:

  ```bash
  wsl.exe -d Ubuntu-20.04 -- ssh ai2 '<command>'
  ```

- `ai2` is the primary target for model-admin and gateway deployment work unless
  the user or current task says otherwise.
- The former `ai3` host is now named `migraine`; use the `migraine` SSH alias.

## Change Workflow

- Start by checking `git status --short`. Preserve user changes and avoid
  unrelated refactors.
- Manage git operations locally in the WSL checkout. Do not run git operations
  from remote Nexus hosts unless the user explicitly asks.
- Keep patches minimal and inspectable.
- Run the relevant tests or validators before committing. For gateway/model
  config changes, at minimum validate JSON/YAML that was touched and run the
  narrow project tests that cover routing, auth, models, and streaming.
- When adding or modifying runnable scripts, set the executable bit before
  commit. Use `chmod +x <path>` where the filesystem supports it, and on Windows
  drvfs use `git update-index --chmod=+x <path>` if needed. Verify with
  `git diff --summary` or `git ls-files --stage <path>`.
- Commit local repo changes when the task calls for a durable checkpoint. Push
  and deploy only when explicitly requested or when the task clearly requires a
  live deployment. If any credential, host, or deployment prerequisite is
  missing, report the exact blocker and the next concrete command or action.

## OpenAI-Compatible Gateway And Continue

- Nexus supports Continue.dev as an OpenAI-compatible provider.
- Preserve tolerant OpenAI-compatible request handling. Unknown OpenAI-style
  fields should be accepted unless they create a security or routing problem.
- Tool-use-shaped fields must not cause empty-body 400 responses. Continue is
  expected to execute tools client-side; Nexus should support the OpenAI
  request/message/response shapes around tool calling and gracefully degrade when
  a backend cannot use tools.
- Preserve existing non-stream chat, streaming chat, `/v1/models`, and bearer
  token behavior when changing gateway compatibility.
- Debug logs around chat-completion requests may include request keys, selected
  model alias, `stream`, tool-field presence, `tool_choice`, and whether tool
  fields were passed through, stripped, or shimmed. Do not log bearer tokens or
  full message content by default.

## ai2 Model Admin Background

Operational context from 2026-06-14 through 2026-06-17 for the ai2
model-admin/gateway repair:

- GLM-5.2 is intentionally resident on `ai2` for the current operating profile.
  In `services/mlx/config/config.example.yaml`, keep
  `mlx-community/GLM-5.2-4bit` at `on_demand: false` unless the user asks to
  return to a memory-saving mode or live memory pressure makes that necessary.
  Do not restore `mlx-community/GLM-5.2-mxfp4` or
  `mlx-community/MiniMax-M3-4bit`; both are excluded from live routing.
- The ai2 native MLX install lives under `/ai-data/var/lib/mlx`. If restarting
  manually, use `MLX_NATIVE_ROOT=/ai-data/var/lib/mlx` unless
  `deploy/scripts/restart-mlx.sh` has already auto-detected that path.

- Aliases `mlx`, `coder`, and `long` are expected to resolve to
  `local_mlx:mlx-community/GLM-5.2-4bit` unless current config and live
  state prove a better route.
- Purge Qwen3 LLM/model and embedding references when requested, but preserve
  Qwen3 TTS. Do not remove, rename, disable, or edit the `qwen3_tts` backend
  unless the user explicitly asks or it is required to protect TTS cache paths.
- Remove stale Qwen3 fallback paths and embedding aliases only after mapping
  current config and live references.
- Do not delete Qwen cache directories blindly. Print candidates, check current
  references, exclude anything used by `qwen3_tts`, and quarantine first under a
  timestamped directory such as
  `/ai-data/runtime/gateway/quarantine/qwen-purge-YYYYMMDD-HHMMSS/`.
- For `local_vllm` and `local_vllm_fast`, determine whether liveness failures
  come from endpoint, model name, stopped service, wrong port, health path,
  stale registry/admin metadata, or model mismatch. If a backend is intentionally
  offline, do not leave it as an active fallback for live aliases.
