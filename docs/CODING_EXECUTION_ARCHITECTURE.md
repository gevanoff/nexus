# Coding Execution Architecture

The Coding Workspace runtime has accumulated several independently useful policies inside one long-lived agent loop: controller escalation, durable state, tool gating, backend routing, backend failover, request formatting, validation, diff review, and semantic acceptance. The failure modes seen in long-horizon runs increasingly occur at the boundaries between those policies rather than inside any one policy.

This document defines a staged architecture that keeps the existing behavior operational while moving the system toward explicit state transitions and backend-independent execution.

## Core invariant

A model call is not the durable unit of work. A **logical coding turn** is.

Before every model call, Nexus should derive an immutable execution policy from durable workspace/controller state, then materialize that logical turn for the selected backend. Backend selection may change without changing the logical turn.

The flow is:

1. Durable workspace/controller state
2. Immutable execution-policy snapshot
3. Backend selection/admission
4. Backend-specific request materialization
5. Model response
6. Tool execution / observation
7. Controller reduction into the next durable state

A retry or failover happens between steps 3 and 4. It must never replay a request serialized for a backend with incompatible capabilities.

## Execution policy snapshot

`coding_execution_policy.py` introduces a small immutable snapshot containing the state that must be coherent for one model call:

- selected backend and upstream model
- native-tool versus text-tool protocol
- forced-action state key and action kind
- exact allowed tool names
- project-plan revision
- deterministic signature

The snapshot is derived immediately before dispatch. This makes tool availability and controller instructions part of the same policy object instead of independently cached values.

## Dispatch boundary

`coding_execution_dispatch.py` treats request formatting as a function of the destination backend.

For Coding Agent requests it:

- refreshes the system prompt from current durable controller state before every dispatch;
- refreshes the native tool allowlist from the same snapshot;
- converts native tool-call history into text-tool history when routing to a backend without native tool support;
- converts tool results into ordinary user messages for text-tool backends;
- removes empty assistant messages that stricter OpenAI-compatible servers reject;
- recomputes the destination route's completion-token cap;
- records execution-policy transitions for debugging;
- performs failover on the logical request and rematerializes it for every selected backend.

This directly addresses failures where an MLX-native-tool request was replayed unchanged to a vLLM text-tool backend.

## Evidence provenance

A successful read is not automatically causal evidence.

`coding_evidence_policy.py` distinguishes:

- **causal evidence**: inspected implementation or configuration targets;
- **acceptance evidence**: tests, fixtures, and examples;
- **context evidence**: documentation and explanatory text.

Forced remediation unlocks editing only when the structured hypothesis explicitly links its `Repository evidence` field to an inspected causal target. Tests may specify expected behavior but cannot, by themselves, establish root cause.

This prevents a negative regression-test fixture from being reinterpreted as the desired implementation.

## Why not rewrite `_run_agent` now?

A top-down rewrite would make the final architecture cleaner, but doing it simultaneously with these correctness fixes would replace too many working invariants at once. The present change instead creates seams that make a later decomposition low-risk.

The recommended migration is:

### Phase 1 — dispatch and policy seams

Implemented by this change.

- immutable execution policy
- backend-aware materialization
- explicit evidence provenance
- transition observability

### Phase 2 — controller as a pure reducer

Move escalation, forced-action transitions, and work-phase changes behind one function:

`next_controller_state(previous_state, observation) -> controller_state`

The reducer should not call models, tools, Git, or persistence directly. It should return the next allowed action class and evidence requirements.

### Phase 3 — turn executor

Extract one iteration of `_run_agent` into a bounded executor:

`execute_turn(task_id, controller_state) -> TurnResult`

`TurnResult` should contain model output, attempted tool calls, tool observations, route transitions, and terminal intent. The outer runner then becomes a small loop over durable reducer + executor transitions.

### Phase 4 — typed observation ledger

Replace inference over loosely structured event history with typed observations for:

- repository evidence
- acceptance evidence
- mutation
- validation
- diff review
- backend transport failure
- model protocol failure
- terminal/blocker decisions

Events remain useful for audit/debug output, but policy should consume typed observations rather than reconstructing semantics from log strings.

### Phase 5 — acceptance pipeline

Make completion a pipeline of explicit predicates:

1. mission completion contract
2. mutation/run-delta contract when required
3. deterministic validation
4. diff review
5. semantic acceptance when appropriate
6. commit/publish

Each predicate should return a typed failure reason and next allowed action instead of pushing prose back into the general agent loop.

## Design rules for future work

- Durable state is authoritative; model conversation history is disposable cache.
- Controller policy and advertised tools must come from one snapshot.
- Backend failover changes transport, never mission semantics.
- Serialization is backend-specific and occurs only after backend selection.
- Tests describe behavior; implementation/configuration evidence establishes causality.
- Retrying a logical turn must not duplicate non-idempotent side effects.
- Every automatic restriction must have a deterministic path to either the next allowed action or a terminal blocker; no state may advertise zero evidence actions while simultaneously requiring new evidence.

These rules make additional backend types, planning/execution separation, richer evidence scoring, and more deterministic smoke testing additive rather than requiring further special cases inside the main agent loop.
