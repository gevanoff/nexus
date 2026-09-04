# Albatross as a Nexus Coding-Harness Control

This document describes the first-stage integration of [Albatross](https://github.com/morganlinton/Albatross) (formerly Small Harness) as an **optional external comparison harness** for the Nexus Coding Workspace.

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

The initial adapter only automates the Albatross side. It emits a harness-neutral result record that can be compared with a Nexus Coding Workspace result once the latter is normalized into the same schema. It intentionally does **not** fake Coding Workspace automation with the existing direct model-eval tool.

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
- bounds the agent and post-run validation under one fixture wall-time deadline;
- launches agent/validation commands in isolated POSIX process groups and terminates descendant processes before collecting final evidence;
- tells Albatross not to commit;
- excludes `.albatross/`, `.small-harness/`, and `.sessions/` from the fixture Git delta;
- copies only explicitly selected evidence into the retained artifact directory;
- sanitizes retained text evidence, including structured trace copies, before returning it;
- **discards the raw execution workspace, isolated HOME, TMPDIR, and all Git object metadata before a run is returned**;
- repairs restrictive owner permissions when deleting execution state and verifies those paths are absent;
- if an unexpected failure occurs before safe retention is established, discards the whole run root and verifies its absence before propagating the failure;
- recomputes the retained diff checksum after sanitization so it identifies the actual evidence file.

This retention boundary is deliberate. Arbitrary workspace/session files are not considered safe artifacts merely because a best-effort redaction pass ran over them. Non-UTF-8 files copied into the retained evidence area are discarded if they cannot be sanitized safely and are listed in `artifacts.omitted_non_text`.

Mutating runs expose a restricted edit/test tool set inside the disposable fixture workspace and omit arbitrary `shell`. Read-only runs, including the live capability probe, expose only `file_read`, `glob`, `grep`, and `list_dir`; write/edit/test tools are removed from the child capability surface rather than relying on prompt compliance.

The adapter passes `--allow-tools` for both mutating and read-only runs so non-interactive tool calls do not block on approval. Authorization still comes from the fixed `AGENT_TOOLS` surface: read-only runs receive only the four inspection tools above, while mutating runs target a disposable fixture workspace with `OUTSIDE_WORKSPACE=deny`. Do not modify the adapter to point mutating tool mode at the live Nexus checkout by default.

Fixture validation commands are trusted executable fixture content. Review new fixtures before running them. Every fixture must define at least one objective result check or validation command; a successful Albatross process alone is not sufficient to mark a fixture complete. Validation launch failures are recorded as failed validation evidence rather than escaping before sanitization.

Process-group isolation currently requires a POSIX host. That covers the intended macOS/ai2 and Linux/WSL environments; the adapter fails closed rather than claiming descendant-process containment on unsupported hosts.

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

The live probe creates a disposable one-file fixture, asks Albatross to read it, and requires a recorded `file_read` tool call. Its Albatross child receives only the read-only tool surface described above. Successful one-shot execution also exercises Albatross's streaming OpenAI client against Nexus SSE behavior.

## Bundled fixtures

List fixtures:

```bash
python3 services/gateway/tools/coding_harness_compare.py list-fixtures
```

The first corpus includes:

- `failure-path-management-link` — modeled on the InvokeAI management-link failure shape; a success-path-only fix is insufficient;
- `validation-after-edit` — a small mutation that needs a targeted post-edit test;
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
- sanitized copies of valid structured Albatross JSONL trace events from explicit Albatross session roots;
- a common `result.json`.

The disposable repository, `.git` objects, Albatross HOME/session source tree, and temporary directory are deleted before `result.json` is returned. This prevents an agent-created commit, oversized workspace file, or raw session artifact from silently preserving the Gateway bearer outside the controlled artifact surface.

The common result separates objective evidence from later semantic judgment. It records outcome, elapsed time, requested Nexus alias, workspace delta, post-run validation, tool/step/context-compaction counts, and artifact paths.

## Compare results

The first-stage adapter compares already-normalized results:

```bash
python3 services/gateway/tools/coding_harness_compare.py compare-results \
  path/to/nexus-result.json \
  path/to/albatross-result.json
```

It is intentionally not yet wired directly to the Coding Workspace API. Automating that side requires preserving Coding Workspace's durable mission/run semantics rather than approximating them with `coding_model_eval.py`.

The next integration step is a **Nexus Coding Workspace result normalizer/runner** that maps one completed or interrupted workspace run into the same result schema, including actual Gateway route evidence. Once that exists, a single command can materialize one fixture twice from the same baseline and execute both harnesses.

## Known first-stage limitation: route receipts

Albatross knows that it requested model alias `coder` through the OpenAI-compatible Nexus endpoint, but its process does not know which Nexus backend/upstream model ultimately served each request. The result therefore leaves `backend` and `upstream_model` empty and explicitly records:

```text
route_evidence = not_available_from_albatross_adapter_v1
```

Do not fill these fields from assumptions. A later Nexus-side request/run correlation mechanism should attach authoritative route receipts.

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
5. recorded actual Nexus route when route correlation becomes available;
6. objective final tests/diff evidence kept separately from model-generated quality judgments.

If the two harnesses use different upstream routes, that run can still be informative, but it is not a clean harness-only comparison and must be labeled accordingly.
