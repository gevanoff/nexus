# Coding Workspace state and convergence

The immutable mission acceptance base remains authoritative across checkpoint
commits, context resets, and runner restarts. A clean working tree can contain a
substantial checkpoint-committed mission delta. Do not infer mission completion
from an empty working-tree file list.

## Control flow and ownership

| Transition | Owner | Lifetime |
| --- | --- | --- |
| Backend response and tool sanitization | `upstreams`, `openai_utils`, trusted policy-rejection capture | Request; context variables isolate concurrent requests |
| Request adaptation, tool extraction and action evaluation | execution dispatch, agent, forced-action policy | Request, derived from current durable state |
| Serialized structured mutation and evidence checks | workspace and plan/edit serialization | Repository and task |
| Mutation provenance and hypothesis consumption | mission acceptance epoch, completion lifecycle | Task; current-run mutation counter is keyed by run ID |
| Bounded coherent edits, validation then review | resume convergence policy | Task; derived action and persisted edit budget |
| Validation freshness and exact diff review | terminal acceptance and mission epoch | Task, bound to latest mutation and mission content |
| Independent semantic acceptance | semantic contract and mission epoch | Immutable mission delta, with durable rejection identity |
| Checkpoint, finalization, interruption and resume | workspace, agent runner, mission epoch | Checkpoints and acceptance persist; runner counters restart |
| Route ranking and admission | guarded admission | Deterministic request ranking filtered by task cooldowns |
| Retry and timeout classification | backend failover and execution dispatch | Transient retry budget is request-local; full read timeouts are task-durable |

Cycle-local messages and counters are not acceptance authority. A context reset
rematerializes controller state. A new run preserves the mission base, validation
provenance, rejection guard, consumed evidence, open edit budget, and cooldowns.

## Delta views

`working_tree` describes uncommitted files. `mission_delta` describes the complete
repository delta from the immutable acceptance base, including checkpoints.
`run_delta` describes the run baseline and observed mutation count, explicitly
labeling legacy runs without a durable counter as unknown. The existing `changes`
field remains available with `scope=working_tree` for compatibility.

These are repository-derived observations, not additional acceptance authorities.
Semantic review continues using the full mission diff and its existing exact
content fingerprint. Snapshots and debug reports expose all three scopes.

## Coherent edit batches

A verified four-field hypothesis that cites two to four causal target files can
open a batch after its first successful mutation. The batch grants at most four
structured edit attempts, including that first mutation and subsequent failed
attempts. It permits only the cited files and targeted reads of those files.
Existing evidence/range checks still apply. Broad search and plan churn are not
batch actions. A materially different hypothesis needs the existing refutation
and grounding process. Ordinary execution can qualify a grounded hypothesis;
stagnation is not a prerequisite. The existing per-task plan/mutation lock owns
budget spending and policy revalidation, including calls authorized before a
concurrent call exhausted the batch.

Every mutation immediately invalidates validation and review. Starting validation
closes the batch early; reaching the attempt limit closes it automatically. For
harness tasks, requesting diff review closes the batch and declared validation
remains the trusted runner's obligation. Refutation and terminal blockers close
the batch. Changing the hypothesis invalidates its authorization. Context resets
and resumed runs never replenish the budget.

After closure the established validation, diff review, and independent semantic
acceptance sequence applies. Unusable reviewers still cause a resumable typed
interruption; genuine semantic rejection reopens repair, and the same rejected
state cannot repeatedly consume reviewer attempts.

## Backend cooldowns

Only the existing full-generation-read-timeout classification creates a cooldown.
The record is keyed by exact backend and upstream-model identity in the task.
The first timeout excludes that lane for 30 minutes, repeated failures increase
the interval to at most two hours, and later success clears the penalty while
retaining recovery evidence. The final failed retry also records the cooldown.
Ordinary transient errors retain request-local retries. Other tasks are unaffected.
Selection, wait and exhaustion events expose the cooldown evidence. Expiry makes
the lane eligible for an ordinary use; there is no unsolicited background probe.
User-selected provider/model routes obey the same task cooldown without silently
switching providers. An older in-flight success cannot erase a newer timeout.

## Terminal and command contracts

Mandatory validation and diff review advertise a blocker-only `coding_finish`
schema. Successful finish is rejected by policy and public dispatch before
semantic review. A concrete `success=false` blocker remains available.

Command argv paths resolve relative to `cwd`; omitted cwd means the repository
root. With `cwd=services/gateway`, pass `app/backends.py`. Paths are not rewritten.

Debug runtime provenance separates recorded mission budgets, configured defaults,
and the effective values persisted when the runner starts. Existing task records
require no migration: missing batch/cooldown state grants no special authority,
and existing acceptance bases are preserved.

## Regression coverage

`services/gateway/tests/test_coding_long_horizon.py` installs the complete
controller over a real temporary Git repository. Its GLM-5.2 to GLM-5.3 fixture
migrates the coder route and MLX defaults while preserving the explicit `glm-5.2`
legacy alias. It exercises multi-file edits, failed and concurrent attempt bounds,
harness validation ownership, a clean checkpoint, rematerialized context,
reviewer unavailability, resumed rejection, fresh refutation evidence, repair,
acceptance and finalization. Backend transport outcomes and reviewer verdicts are
scripted; the test does not wait for a real 600-second upstream timeout or run a
live semantic model. Existing protocol and acceptance tests remain required.
