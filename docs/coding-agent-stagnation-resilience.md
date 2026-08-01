# Coding Agent Stagnation Resilience

## Problem

Long-horizon coding runs can spend repeated cycles reconstructing the same repository understanding without producing an edit, validation transition, diff-review transition, blocker, or finish decision. Context compaction and continuation can amplify this failure by discarding raw event detail while preserving too little semantic state.

The existing cycle-boundary checkpoint path remains the authoritative delivery mechanism. This design adds a durable controller state around it so the same unchanged repository state cannot repeatedly mint fresh recovery opportunities.

## Durable output state

The controller derives a stable state key from output-bearing transitions only:

- workspace fingerprint;
- validation revision;
- diff-review revision;
- finish state.

Plan and guidance revisions are deliberately excluded. They remain useful context, but note-only plan churn and controller guidance are not repository progress.

## Persisted controller records

Active tasks may contain these backward-compatible optional fields:

- `agent_stagnation_controller`: state key, monotonic no-outcome cycle count, classification, intervention stage, thresholds, and bounded intervention history;
- `agent_inspection_ledger`: normalized read, search, tree, review, and validation signatures with occurrence counts and recency;
- `agent_working_memory`: bounded findings, inspected targets, unresolved question, exactly one next action, blocker, revision, and provenance;
- `agent_context_manifest`: source event counts, omitted event counts, preserved semantic sections, and a manifest hash;
- `agent_stagnation_recovery_lease`: one state-keyed recovery transition for a terminal intervention or a continuation after `no_progress_limit`.

Older task JSON remains valid because all new fields are optional and derived lazily.

## Semantic loop detection

Exact command equality is insufficient. The controller normalizes behavior:

- adjacent line reads of the same file share one inspection signature;
- searches are grouped by normalized path and stable query tokens;
- status, diff, and change-summary calls are review behavior;
- validation commands are grouped by normalized argv hash.

The controller classifies stagnant runs as inspection, review, validation, reasoning, plan-churn, or generic execution loops.

## Staged intervention

Thresholds are derived from the mission's `max_no_progress_cycles` budget:

1. **Observe**: persist controller state and provenance without injecting guidance.
2. **Assist**: persist working memory and require one bounded next action.
3. **Interrupt**: require the next cycle to edit, validate/review an edit, or finish with a blocker.
4. **Recovery**: grant one state-keyed recovery intervention at the terminal boundary.

A continuation after `no_progress_limit` receives one distinct continuation recovery for the same state. Generic restarts do not receive fresh intervention credit.

## Controller guidance is not progress

Controller messages are appended to `guidance_messages` so the running model receives them, but they update `last_controller_guidance_at` rather than `last_guidance_at`. This prevents the checkpoint itself from resetting the no-progress counter.

User-authored guidance continues to use the existing guidance timestamp and behavior.

## Compaction and restart

Working memory and the context manifest are persisted independently of the bounded raw event window. The rendered checkpoint includes:

- established findings, with assistant-derived claims explicitly marked unverified;
- normalized inspected targets;
- the unresolved question;
- exactly one required next action;
- a concrete blocker when present;
- counts of preserved and omitted events.

This makes compaction and continuation deterministic without pretending that a lossy summary is the original transcript.

## Invariants

- One intervention ID is claimable once per durable state and stage.
- One plan checkpoint is allowed per unchanged output state.
- Plan or guidance churn cannot create a new durable state key.
- Same-state generic restarts do not receive fresh credit.
- Same-state continuations after `no_progress_limit` receive one distinct bounded recovery.
- Workspace, validation, diff-review, or finish transitions produce a new state key and reset the controller naturally.
- Background scanner and synchronous cycle-boundary claims remain idempotent through the atomic task mutation.

## Validation

Focused regression coverage verifies:

- state-key stability under plan and guidance churn;
- semantic coalescing of adjacent reads;
- stagnation persistence across run restart;
- controller guidance delivery without progress minting;
- one plan checkpoint per unchanged output state;
- one no-progress continuation recovery;
- compaction provenance and working-memory preservation.
