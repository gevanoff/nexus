# Albatross as a Nexus Coding-Harness Control

This document describes the integration of [Albatross](https://github.com/morganlinton/Albatross) (formerly Small Harness) as an **optional external comparison harness** for the Nexus Coding Workspace.

Albatross is not a production Nexus backend and does not replace Coding Workspace control, mission acceptance, validation gates, resumability, finalization, or publication. Its purpose here is experimental: hold the repository task and Nexus inference plane as constant as practical while changing the agent harness.

## Tested upstream

The initial adapter was designed against:

- Albatross `v2.4.0`
- commit `6f20178d81c6f0fdbb97ccf826b0d56f04a77faf`
- upstream release date: 2026-08-25

The run result records both the installed version and this tested baseline. Newer Albatross releases may work, but a changed version is part of the experimental evidence and should not be silently treated as equivalent.

Albatross v2.4.0 supports an OpenAI-compatible provider via `OPENAI_BASE_URL`, one-shot execution via `--print`, external agent eval fixtures via `--eval`, JSON eval output via `--json`, tool auto-approval for non-interactive runs via `--allow-tools`, and JSONL turn traces.

## Architecture

```text
                   common task / fixture
                           |
              +------------+------------+
              |                         |
              v                         v
     Nexus Coding Workspace          Albatross
              |                         |
              |                         | OpenAI-compatible
              +------------+------------+
                           v
                     Nexus Gateway
                        /v1
                           |
                    MLX / vLLM / ...
```

The adapter automates both sides and emits the same harness-neutral result schema. The Nexus side creates a durable Coding Workspace task through the Coding API, executes the normal agent and finalization lifecycle, and normalizes its persisted run evidence. It intentionally does **not** fake Coding Workspace automation with the existing direct model-eval tool.

## Security model

`services/gateway/tools/coding_harness_compare.py` does not accept a live repository path for Albatross execution. A fixture contains inline source files, and the adapter materializes those files into a new disposable Git repository under the result root.

For each run the adapter:

- creates an isolated Git workspace;
- creates an isolated `HOME` and `TMPDIR`;
- passes only a small allowlist of inherited environment variables;
- does not inherit GitHub, AWS, SSH-agent, or unrelated provider credentials;
- supplies the Nexus bearer token only as `OPENAI_API_KEY` in the child environment;
- sets `BACKEND=openai` and `OPENAI_BASE_URL` to Nexus;
- sets `OUTSIDE_WORKSPACE=deny`;
- skips the Albatross setup wizard and update check;
- bounds the agent and post-run validation under one fixture wall-time deadline, then gives trace collection a separate ten-second post-run budget so normalized timeout evidence can still be retained;
- completes output-redaction preparation before launch, launches agent commands under Linux subreaper supervision and post-run validation against a read-only workspace and root inside a Bubblewrap filesystem/network sandbox, places validation scratch space on a private tmpfs with 64 MiB and 4,096-inode kernel-enforced quotas (including deleted-open files), and enforces additional file-size, descriptor, 128-task, 16 GiB per-process address-space, and 2 GiB aggregate resident-memory ceilings before terminating complete adopted descendant trees even on operator interruption and collecting final evidence;
- tells Albatross not to commit;
- excludes `.albatross/`, `.small-harness/`, and `.sessions/` from the fixture Git delta;
- decodes raw Git path streams with POSIX `surrogateescape` so non-UTF-8 filename bytes are not replaced before evidence capture;
- builds final Git evidence from an independent baseline index with pinned worktree and file-mode settings so agent-controlled assume-unchanged, skip-worktree, local comparison, and ignore settings cannot hide workspace changes;
- copies only explicitly selected evidence into the retained artifact directory;
- sanitizes disposable Git config and installs highest-priority neutral worktree attributes before snapshotting so agent-defined hooks, filters, encodings, `ident`, and comparison settings cannot execute or hide raw changes during evidence collection;
- enforces aggregate changed-file count, file-byte, diff-size, snapshot-time, trace-entry, trace-file, trace-time, and fixture-configured trace-step limits before retaining evidence;
- retains enough pre-tail overlap for ordinary protected-value encodings, detects raw and hexadecimal values split by permitted whitespace and raw or encoded values reconstructed across path components, redacts raw and encoded values before applying the final stdout/stderr tail bound and again across the complete result, redacts truncated output wholesale whenever protected values are in scope, and records `artifacts.process_output_truncated` when earlier data was omitted;
- captures structured traces from the isolated Albatross home before validation, runs validation with a separate sandbox home and Python bytecode generation disabled, and sanitizes retained text evidence before returning it;
- writes a harness-owned `agent.config.json` before launch that directs Albatross's `sessionDir` into that isolated home, keeping trace evidence outside the agent-writable workspace while excluding the temporary config from the measured Git delta;
- prefers Albatross event sidecars and, because tested v2.4.0 one-shot runs do not wire the primary agent trace into those sidecars, otherwise derives only completed tool names and step counts from the private structured session transcript; retained fallback evidence contains no prompt, tool arguments, tool output, or response text;
- **discards the raw execution workspace, isolated HOME, TMPDIR, and all Git object metadata before a run is returned**;
- repairs restrictive owner permissions when deleting execution state and verifies those paths are absent;
- rejects unexpected entries anywhere inside the retained run hierarchy, leaving only the controlled artifact directory after execution cleanup;
- if an unexpected failure occurs before safe retention is established, discards the whole run root and verifies its absence before propagating the failure;
- recomputes the retained diff checksum after sanitization so it identifies the actual evidence file.

This retention boundary is deliberate. Arbitrary workspace/session files are not considered safe artifacts merely because a best-effort redaction pass ran over them. Protected values are checked in raw, UTF, standard and URL-safe Base64, Base32/Base32-hex, Base85/Ascii85, hexadecimal, and common character-escape forms, including supported padded, unpadded, case-folded, and whitespace-wrapped variants; encoded values in paths are replaced with generic metadata and are never copied. Non-UTF-8 files copied into the retained evidence area are discarded if they cannot be sanitized safely and are listed in `artifacts.omitted_non_text`.

Mutating runs expose a restricted read/edit tool set inside the disposable fixture workspace and omit arbitrary `shell` and `run_tests`. Tests run only through the declared post-run validation commands inside the OS sandbox, preventing agent-launched test code from forging Albatross trace files or accessing operator state. Read-only runs, including the live capability probe, expose only `file_read`, `glob`, `grep`, and `list_dir`; write/edit tools are removed from the child capability surface rather than relying on prompt compliance.

The adapter passes `--allow-tools` for both mutating and read-only runs so non-interactive tool calls do not block on approval. Authorization still comes from the fixed `AGENT_TOOLS` surface: read-only runs receive only the four inspection tools above, while mutating runs target a disposable fixture workspace with `OUTSIDE_WORKSPACE=deny`. Do not modify the adapter to point mutating tool mode at the live Nexus checkout by default.

Fixture validation commands are trusted executable fixture content. Review new fixtures before running them. Every fixture must define at least one objective result check or validation command; a successful Albatross process alone is not sufficient to mark a fixture complete. Validation launch failures are recorded as failed validation evidence rather than escaping before sanitization.

Fixture missions are limited to 64,000 UTF-8 bytes because Albatross receives the mission as one `--print` argument. Before materializing a run, the adapter also verifies that the installed binary's help output advertises both `--print` and `--allow-tools`; incompatible binaries fail before any fixture execution.

Complete descendant-process containment currently requires Linux `prctl` subreaper support and readable procfs child enumeration. Validation additionally requires Bubblewrap (`bwrap`) for a private mount, PID, user, and network namespace. On Ubuntu/Debian install it with `sudo apt-get install bubblewrap`. The adapter fails closed before unsafe validation when these controls are unavailable, including on macOS and other hosts, rather than allowing validation code to access operator files or the network. Credential-free offline `--version` and `--help` probes remain portable by falling back to bounded process-group cleanup when procfs enumeration is unavailable. Run comparison fixtures from a compatible Linux/WSL environment until equivalent macOS containment is implemented.
Run the adapter as a non-root host user: the validation task ceiling relies on `RLIMIT_NPROC`, which Linux does not enforce for host UID 0, so validation fails closed before launch when the adapter itself is root.

## Install Albatross

On macOS hosts such as ai2, use Albatross's upstream-supported Homebrew installation separately from Nexus deployment:

```bash
brew install morganlinton/tap/albatross
albatross --version
```

On other operating systems, use the installation method supported by the upstream Albatross release rather than assuming Homebrew is available.

The tested baseline is `2.4.0`. For a comparison run, record the actual version shown by the adapter. Do not make normal Nexus startup, deployment, or CI install Albatross.

## Nexus configuration

Set the Nexus credential in the shell that launches the adapter:

```bash
export NEXUS_BASE_URL=http://ai2:8800/v1
export NEXUS_API_KEY='<gateway bearer token>'
```

`GATEWAY_BEARER_TOKEN` is also accepted. The adapter deliberately does **not** consume a pre-existing `OPENAI_API_KEY` as its Nexus credential, which avoids accidentally forwarding a real OpenAI cloud key to the local Gateway.

The initial model alias defaults to `coder`.

## Capability probe

Offline capability inspection:

```bash
python3 services/gateway/tools/coding_harness_compare.py probe
```

This checks installation/version and the CLI surfaces required by this adapter. The probe exits nonzero if the installed binary is missing a required surface rather than merely reporting an incompatible capability while claiming success.

A live, read-only Albatross→Nexus probe is opt-in:

```bash
python3 services/gateway/tools/coding_harness_compare.py probe --live --model coder
```

The live probe creates a disposable one-file fixture, asks Albatross to read it, and requires a recorded `file_read` tool call. Its Albatross child receives only the read-only tool surface described above. The probe reports streaming capability as unknown because the retained execution evidence does not distinguish SSE transport from a non-streaming completion.

## Bundled fixtures

List fixtures:

```bash
python3 services/gateway/tools/coding_harness_compare.py list-fixtures
```

The first corpus includes:

- `failure-path-management-link` — modeled on the InvokeAI management-link failure shape; a success-path-only fix is insufficient;
- `validation-after-edit` — a small mutation that needs a targeted post-edit test and preserves non-string numeric compatibility;
- `cross-file-policy-fix` — the visible behavior is in one file but the causal default belongs in another.

The schema is JSON and intentionally harness-neutral. A fixture contains:

- an id and description;
- inline repository files;
- the substantive mission;
- objective changed-file/content expectations;
- post-run validation argv arrays;
- bounded wall time and agent steps;
- tags for later analysis.

The current v1 schema uses inline files specifically to prevent an eval fixture from silently pointing Albatross at a live checkout.
Fixture paths are validated once by the shared loader before either harness starts;
backslashes, control characters, empty path components, traversal components, and
harness-owned metadata paths are rejected consistently.

The Nexus runner submits those same inline files to the bearer-authenticated `POST /v1/coding/harness/runs` endpoint. That endpoint:

- accepts only bounded text files with safe repository-relative paths;
- creates a local-only Git repository with a `main` baseline and no remote;
- marks the task as `harness_eval` and forces publication off;
- starts the normal durable Coding Workspace runner and finalization path;
- gives the disposable task an expiry lease equal to its run budget plus 15
  minutes (capped at 24 hours), after which monitoring removes settled terminal
  evaluations and initializing/idle or ready/idle evaluations abandoned before
  agent startup;
- exposes a harness-only validation route that honors the accepted run budget
  (up to one hour) without attaching Git credentials, and blocks deletion while
  validation or an agent tool worker remains active;
- exposes strict text-or-binary file evidence using the same 2 MB per-file cap
  as the shared fixture schema;
- permits `DELETE /v1/coding/harness/tasks/{task_id}` only for a terminal harness task, never a normal Coding Workspace task.

The client collects the baseline diff, selected final text files, post-run validation, persisted route/run evidence, and then deletes the server-side task and Git workspace. It rejects a truncated Gateway diff instead of hashing partial evidence, parses change paths from NUL-delimited Git output, deduplicates paths before enforcing the changed-file limit, and adds deterministic unified patches with the real executable mode for text-only untracked files. A harness-only file endpoint preserves exact whitespace in paths and uses strict UTF-8 decoding; binary files and paths containing symlinks are identified and recorded as evidence omissions instead of being replacement-decoded or dereferenced into fabricated evidence. Diff, change, and file endpoints return a conflict rather than sample an active worker's changing worktree. Final-file collection has a 30-second aggregate time bound after any guarded worker has settled; that bound is excluded from the validation budget, and its 16 MB aggregate byte budget is enforced before each response is cached. The client does not report `execution_workspace_retained: false` until deletion is confirmed, and cleanup retries conflicts through at least the Gateway's default 120-second command lifetime. A failed client run also discards its partial local evidence. If server-side deletion cannot be confirmed, the command fails closed with a `SECURITY` error.

Native harness Git evidence is collected through a disposable Git directory and
independent baseline index. Repository/global Git config, worktree attributes,
and agent-controlled index flags therefore cannot hide or transform edits relative
to the Albatross snapshot. Validation argv payloads are likewise preserved exactly;
only the executable name and command policy are normalized for authorization.

Coding Workspace validation uses the existing task command policy and execution environment; it is not the Bubblewrap sandbox used for the external Albatross child. Fixture validation commands are trusted executable content on both paths and must be reviewed before use.

Coding Workspace enforces a minimum run horizon of four agent cycles and 60 seconds. Native and paired commands reject fixtures below either minimum instead of silently widening one side of the comparison.

## Run Albatross through Nexus

Example:

```bash
python3 services/gateway/tools/coding_harness_compare.py run-albatross \
  --fixture services/gateway/tools/coding_harness_fixtures/failure-path-management-link.json \
  --model coder
```

Artifacts default under:

```text
${NEXUS_RUNTIME_ROOT:-.runtime}/coding-harness-evals/
```

Each retained run keeps only selected evidence:

- redacted stdout/stderr;
- the sanitized final Git diff;
- sanitized final copies of changed text files up to the capture limit;
- sanitized copies of valid structured Albatross JSONL trace events from explicit Albatross session roots, or a minimal normalized event record derived from the private session transcript when the tested one-shot trace sidecar is absent;
- a common `result.json`.

The disposable repository, `.git` objects, Albatross HOME/session source tree, and temporary directory are deleted before `result.json` is returned. This prevents an agent-created commit, oversized workspace file, or raw session artifact from silently preserving the Gateway bearer outside the controlled artifact surface.

The common result separates objective evidence from later semantic judgment. It records outcome, elapsed time, requested Nexus alias, workspace delta, post-run validation, tool/step/context-compaction counts, and artifact paths.

## Run Nexus Coding Workspace

The native runner requires a Gateway deployment that includes the harness endpoints:

```bash
python3 services/gateway/tools/coding_harness_compare.py run-nexus \
  --fixture services/gateway/tools/coding_harness_fixtures/failure-path-management-link.json \
  --model coder
```

The Gateway bearer token is read from the same `NEXUS_API_KEY` or `GATEWAY_BEARER_TOKEN` variables. The normalized result records `backend` and `upstream_model` from the durable run record and includes bounded start/reroute history from Coding Workspace events. Its `route_evidence` value is:

```text
coding_workspace_persisted_run_record
```

Coding Workspace exposes the last 80 public events rather than a raw harness trace. The result labels that event-window limit explicitly; step count and final route still come from the durable run record.

## Run a paired comparison

One command can materialize and execute the same fixture through both harnesses:

```bash
python3 services/gateway/tools/coding_harness_compare.py run-paired \
  --fixture services/gateway/tools/coding_harness_fixtures/failure-path-management-link.json \
  --model coder
```

The command creates a pair directory, writes both normalized `result.json` files, prints the comparison table, and writes `comparison.json` with both result paths. It exits successfully only when both objective-normalized runs complete. Use `--json` for machine-readable combined output.

## Compare saved results

The adapter can also compare already-normalized saved results:

```bash
python3 services/gateway/tools/coding_harness_compare.py compare-results \
  path/to/nexus-result.json \
  path/to/albatross-result.json
```

## Known limitation: Albatross route receipts

Albatross knows that it requested model alias `coder` through the OpenAI-compatible Nexus endpoint, but its process does not know which Nexus backend/upstream model ultimately served each request. The result therefore leaves `backend` and `upstream_model` empty and explicitly records:

```text
route_evidence = not_available_from_albatross_adapter_v1
```

Do not fill these fields from assumptions. The native Coding Workspace result has authoritative persisted route evidence, but a later Nexus-side request/run correlation mechanism is still needed to attach equivalent receipts to the Albatross result.

## Selective Albatross architecture review

The following upstream mechanisms are worth comparing, not automatically copying:

| Mechanism | Initial classification | Nexus implication |
| --- | --- | --- |
| Bounded evaluator subsystem | **adopt soon** | Strong model for keeping reviewer failures inside one terminal operation instead of burning author cycles. |
| Independent read-only critic | **already equivalent / investigate** | Nexus semantic acceptance has stronger mission semantics; Albatross's fixed read-only surface is a useful authority reference. |
| Generator/evaluator separation | **already equivalent** | Preserve and strengthen independent routing/failover rather than collapse roles. |
| Explicit reset/handoff vs compaction | **investigate experimentally** | Compare long-horizon coherence against Nexus durable snapshot reconstruction before changing context policy. |
| Explicit autonomous stop reasons | **adopt soon** | Nexus already has typed stop reasons; continue consolidating them into an explicit authority/result model. |
| Stall detection | **already equivalent** | A/B fixtures can reveal whether Nexus progress fingerprints outperform diff/score heuristics. |
| Routing receipts | **adopt soon** | Nexus routing is richer, but experiment/debug output needs clearer candidate/selection/failure evidence. |
| `/doctor --deep` capability probing | **adopt soon** | Useful pattern for model/backend tool/stream/structured-output compatibility checks. |
| Agent trajectory eval fixtures | **adopt soon** | Highest-value lesson: emergent controller trajectories should become executable fixtures, not only unit tests. |
| JSONL event/trace model | **already equivalent / investigate** | Compare usability and ensure Nexus debug reports preserve enough structured route/controller evidence. |
| Lifecycle hooks/extensions | **investigate experimentally** | Could eventually reduce wrapper accumulation around `_run_agent`, but is not part of this first integration. |
| Malformed tool-call recovery | **investigate experimentally** | Compare failure recovery empirically across the same underlying model routes. |
| Fixed verification surface | **adopt soon** | Supports independent critics without granting arbitrary mutation/shell authority. |
| Albatross as production Coding Workspace engine | **not appropriate now** | Nexus owns durable mission contracts, controller authority, publication, and resumability. |

## Experimental discipline

A useful A/B comparison must control what it can:

1. same fixture baseline;
2. same substantive mission;
3. same requested Nexus model alias;
4. recorded Albatross/Nexus versions;
5. recorded actual Nexus route for Coding Workspace, with the Albatross route left explicitly unavailable until request correlation exists;
6. objective final tests/diff evidence kept separately from model-generated quality judgments.

If the two harnesses use different upstream routes, that run can still be informative, but it is not a clean harness-only comparison and must be labeled accordingly.
