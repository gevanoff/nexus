# Local Coding Model Tuning

This document records the practical starting point for Nexus local coding models.
It is intentionally focused on real coding work: repository debugging, patch
generation, test repair, code review, and multi-file reasoning.

## Live Inventory

Observed from the local Nexus hosts, with hardware refreshed on 2026-05-21:

| Host | Runtime | Live model/service | Practical role | Notes |
| --- | --- | --- | --- | --- |
| `ai2` | MLX, native macOS | `mlx-community/Qwen3.6-27B-4bit`, `mlx-community/Qwen3-30B-A3B-4bit`, `mlx-community/bge-small-en-v1.5-8bit` | Current best `coder` path | Apple M3 Ultra, 512 GB unified memory. Best control-plane fit for long local coding sessions. |
| `stackrot` | vLLM | `unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M` at `max_model_len=2048`; `BAAI/bge-small-en-v1.5` | Fast short edits and embeddings | Intel Core i7-12700F, about 46 GiB observed RAM, 2x RTX 3090 24 GB. Schedule by per-GPU 24 GB VRAM limits. |
| `ada2` | vLLM | `unsloth/Qwen3-30B-A3B-FP8` at `max_model_len=2048` | Strong CUDA lane, currently too short for repo coding | RTX 6000 Ada 48 GB class / 46 GiB reported VRAM. Media services share this GPU, so long-context coding needs an explicit resource window. |
| Gateway | OpenAI-compatible | aliases include `coder`, `fast`, `long`, `reasoning`, `local_mlx`, `local_vllm`, `local_vllm_fast` | Routing and caps | Coding agent should default to the most reliable tool-capable backend, not the fastest chat lane. |

Primary source facts used for candidates:

- Qwen3-Coder-30B-A3B-Instruct is a 30.5B total / 3.3B active MoE coding model with 256K native context and vLLM usage documented on its model card: <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct>.
- Qwen2.5-Coder-32B-Instruct is a 32.5B dense code model; its card lists 131,072 token long-context support and notes the shipped config is set to 32,768 without YaRN: <https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct>.
- DeepSeek-Coder-V2-Lite-Instruct is a 16B total / 2.4B active MoE coding model with 128K context: <https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct>.
- Qwen3-30B-A3B is not coding-specialized; its card warns against greedy decoding for thinking mode and recommends nonzero sampling: <https://huggingface.co/Qwen/Qwen3-30B-A3B>.
- vLLM exposes the runtime controls this stack needs, including `max_model_len`, `gpu_memory_utilization`, `kv_cache_dtype`, prefix caching, `max_num_batched_tokens`, and `max_num_seqs`: <https://docs.vllm.ai/en/latest/configuration/engine_args/>.
- MLX LM's OpenAI-like server supports `max_tokens`, `temperature`, `top_p`, `top_k`, `min_p`, repetition, presence, and frequency penalties: <https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md>.
- Ollama Modelfile parameters include `num_ctx`, `repeat_penalty`, `temperature`, `top_k`, `top_p`, `min_p`, `seed`, `stop`, and `num_predict`: <https://docs.ollama.com/modelfile>.

## Ranked Model/Runtime Table

This is the initial ranking to validate with `services/gateway/tools/coding_model_eval.py`.
It ranks expected practical usefulness on the available hardware, not synthetic
benchmark claims.

| Rank | Model/runtime | Fit | Recommended use | Why | Watch-outs |
| --- | --- | --- | --- | --- | --- |
| 1 | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` on `ada2` vLLM | Target install | `coding_repo`, test repair, agentic patch loops | Coding-specific, MoE efficiency, huge native context, vLLM-friendly | Needs a dedicated coding profile and more than the current 2K context. Validate FP8 quality and tool-call format. |
| 2 | `mlx-community/Qwen3.6-27B-4bit` on `ai2` MLX | Live | Default `coder` until Qwen3-Coder is deployed | Already available, large unified memory, tool-capable MLX route | Long-context prefill latency and possible MLX prompt-cache behavior under agent loops. |
| 3 | `Qwen/Qwen2.5-Coder-32B-Instruct` 4-bit/8-bit on MLX or quantized vLLM | Candidate | Conservative dense fallback for repo debugging | Proven coding model family and strong instruction behavior | Dense 32B is slower and heavier than 30B-A3B MoE; use 32K first before YaRN. |
| 4 | `deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct` GGUF/Ollama or vLLM if supported | Candidate | `coding_fast`, small fixes, low-latency review | Small active parameter count and 128K context | Older model; function/tool behavior and dependency freshness need eval. |
| 5 | `unsloth/Qwen3-30B-A3B-FP8` on `ada2` vLLM | Live | General reasoning and architecture notes | Strong local reasoning/chat lane | Not coding-tuned and currently served at 2K context, which is inadequate for repo coding. |
| 6 | `unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M` on `stackrot` vLLM | Live | Quick explanations and tiny edits | Fast lane is already online | 2K context, Q4 quality, and non-coder training make it risky for multi-file edits. |

## Recommended Profiles

Use these as Gateway/request profiles. Do not force temperature 0 as the default;
keep it as a diagnostic probe because Qwen3-family model cards explicitly warn
that greedy decoding can degrade behavior for thinking paths.

### `coding_fast`

Use for autocomplete, small bug fixes, and quick explanations.

```json
{
  "temperature": 0.15,
  "top_p": 0.9,
  "top_k": 40,
  "min_p": 0.05,
  "repetition_penalty": 1.05,
  "frequency_penalty": 0.0,
  "presence_penalty": 0.0,
  "max_tokens": 4096,
  "seed": 1234,
  "attempts": 1,
  "stop": []
}
```

### `coding_repo`

Use for multi-file edits, debugging, test-driven patching, and repo questions.
This should be the default coding workspace profile.

```json
{
  "temperature": 0.2,
  "top_p": 0.85,
  "top_k": 40,
  "min_p": 0.03,
  "repetition_penalty": 1.03,
  "frequency_penalty": 0.0,
  "presence_penalty": 0.0,
  "max_tokens": 8192,
  "seed": 1234,
  "attempts": 2,
  "stop": []
}
```

### `coding_reasoning`

Use for architecture, subtle bugs, repeated failed tests, and hard reasoning.

```json
{
  "temperature": 0.3,
  "top_p": 0.95,
  "top_k": 20,
  "min_p": 0.02,
  "repetition_penalty": 1.02,
  "frequency_penalty": 0.0,
  "presence_penalty": 0.0,
  "max_tokens": 16384,
  "seed": 1234,
  "attempts": 2,
  "stop": []
}
```

Also run a `greedy_probe` with `temperature=0`, `top_p=1`, `top_k=0`,
`min_p=0`, and `repetition_penalty=1.0`. Keep it only if the eval shows
stable patches, no repetition loops, and no brittle failures.

## Runtime Settings

### `ada2` vLLM strong coding lane

Current live context is 2K, which is not enough for coding workspaces. The best
target is a dedicated coding window where media-heavy services are stopped or
deprioritized.

Recommended target:

```text
model: Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8
served_model_name: qwen3-coder-30b-a3b
max_model_len: 32768 initially; test 65536 after stability passes
gpu_memory_utilization: 0.72 dedicated, 0.55-0.65 when sharing with media
cpu_offload_gb: 8
tensor_parallel_size: 1
kv_cache_dtype: fp8 for capacity tests, auto for quality control
max_num_batched_tokens: 16384 initially; 32768 for dedicated long-context runs
max_num_seqs: 1 for coding agents, 2 only after latency and KV pressure are acceptable
enable_prefix_caching: true
timeout_sec: 600 for repo tasks
structured/tool support: enable only after Qwen tool-call format is verified through Gateway
```

### `stackrot` vLLM fast lane

The fast lane should stay latency-oriented.

```text
model: Qwen3-Coder 30B GGUF or DeepSeek-Coder-V2-Lite GGUF if vLLM support is stable; otherwise keep current Qwen3-30B-A3B GGUF
max_model_len: 8192 for fast coding; 2048 only for non-coding chat
gpu_memory_utilization: 0.78-0.82 on the selected 24 GB GPU
tensor_parallel_size: 1
kv_cache_dtype: auto first; test fp8 only after correctness is stable
max_num_batched_tokens: 4096-8192
max_num_seqs: 1-2
enable_prefix_caching: true
timeout_sec: 180
```

### `ai2` MLX coding lane

```text
model: mlx-community/Qwen3.6-27B-4bit today; evaluate MLX Qwen3-Coder-30B-A3B when available
context: 32768 for coding_repo; 65536 only after long-context stability tests
max_tokens: 8192 coding_repo, 16384 coding_reasoning
temperature/top_p/top_k/min_p: use the profiles above
prompt caching: enable or preserve if the server/runtime exposes it
batch/concurrency: keep coding agents at single-request priority; avoid multi-model eager loading if memory pressure rises
memory policy: monitor resident memory during long-context prefill, not only generation
```

### Ollama / llama.cpp

Use this lane for GGUF candidates and as a reliability fallback.

```text
num_ctx: 8192 coding_fast, 32768 coding_repo, 65536 only after stability passes
num_predict: 4096 fast, 8192 repo, 16384 reasoning
num_gpu: all available layers that fit without paging
num_batch: 512 start, reduce if prompt processing fails
repeat_penalty: 1.03-1.05 for code; avoid high penalties that corrupt identifiers
top_k/top_p/min_p/temperature: match the profile
mmap: true
mlock: true only when RAM headroom is guaranteed
stop: avoid broad Markdown stops; prefer tool/protocol stops only for a specific agent protocol
```

## Gateway Alias Recommendations

Keep aliases role-based so model swaps do not require UI or agent changes.

```json
{
  "coding_fast": {
    "backend": "local_vllm_fast",
    "model": "qwen3-coder-30b-a3b"
  },
  "coding_repo": {
    "backend": "local_mlx",
    "model": "mlx-community/Qwen3.6-27B-4bit"
  },
  "coding_reasoning": {
    "backend": "local_mlx",
    "model": "mlx-community/Qwen3.6-27B-4bit"
  },
  "coder": {
    "backend": "alias",
    "model": "coding_repo"
  },
  "coder-gpu": {
    "backend": "local_vllm",
    "model": "qwen3-coder-30b-a3b"
  },
  "coder-greedy-probe": {
    "backend": "local_mlx",
    "model": "mlx-community/Qwen3.6-27B-4bit"
  }
}
```

If the Gateway alias schema does not support alias-to-alias entries, make
`coder` point directly at the same target as `coding_repo`.

Gateway caps and policies:

```text
CODING_AGENT_MAX_TOKENS=8192
CODING_AGENT_TOOL_CONTEXT_CHARS=32000
CODING_AGENT_MAX_TOOL_RESULT_CHARS=100000
CODING_COMMAND_TIMEOUT_SEC=180 for repo evals; 120 is fine for small tasks
tool policy: keep destructive git commands blocked; require validation and coding_git_diff after edits
fallback: do not silently fall back from coding_repo to a non-tool-capable vLLM backend for autonomous work
streaming: keep enabled for TTFT measurement and UI feedback
```

## Agent Workflow

Recommended coding-agent contract:

1. Inspect with `rg`/tree first; read targeted line ranges.
2. Search for symbols before using them. Do not invent functions, settings, or paths.
3. Prefer exact replacements or unified patches over whole-file rewrites.
4. After the latest edit, run a targeted validation command.
5. Inspect `coding_git_diff` after the latest edit.
6. If validation fails, incorporate the failure into one repair loop.
7. Report success only after validation and diff review.

The Gateway now enforces the last two requirements for successful autonomous
coding runs. It should still be evaluated with a verifier pass: a second model
or deterministic checker should review the final diff for unrelated edits,
hallucinated symbols, broad docs rewrites, and missed test failures.

## Known Failure Modes

| Model/runtime | Failure mode to test |
| --- | --- |
| Qwen3-Coder-30B-A3B | May over-trust huge context and skip retrieval discipline; validate tool-call formatting and long-context truncation behavior. |
| Qwen3.6-27B MLX | Long prompt prefill can dominate latency; watch for repeated library imports or invented symbols when context is thin. |
| Qwen2.5-Coder-32B | Dense model may be slower; 128K/YaRN settings can hurt quality if used when 32K is enough. |
| DeepSeek-Coder-V2-Lite | Good speed but older coding knowledge; can produce plausible but stale dependency/config advice. |
| Qwen3-30B-A3B chat | Not coding-specific; can reason well but make non-minimal patches or miss project conventions. |
| GGUF Q4 fast lanes | Quantization can cause subtle identifier corruption and brittle long-context behavior. |

## Defaults By Task

| Task | Default |
| --- | --- |
| Autocomplete | `coding_fast` on `local_vllm_fast` after a coding model is installed; otherwise MLX `coder` with shorter context. |
| Small edits | `coding_fast`, one rollout, targeted tests required. |
| Repo debugging | `coding_repo` on MLX now; move to Qwen3-Coder on `ada2` after eval passes. |
| Architecture | `coding_reasoning` on MLX or Qwen3-Coder long-context vLLM. |
| Long-context analysis | Qwen3-Coder target on `ada2` at 32K/64K, or MLX at 32K if latency is acceptable. |
| Test repair | `coding_repo`, two attempts, validation/diff gate required. |

## Eval Harness

The repeatable direct-patch harness is:

```powershell
$env:GATEWAY_BEARER_TOKEN = "<token>"
python services/gateway/tools/coding_model_eval.py `
  --base-url http://ai2:8800/v1 `
  --model coder `
  --profile coding_repo `
  --runtime mlx `
  --host ai2 `
  --quantization 4bit `
  --context-length 32768 `
  --stream `
  --out .runtime/coding-model-evals/coder-mlx.jsonl
```

The bundled suite covers:

1. Single-file bug fix
2. Multi-file bug fix
3. Failing test repair
4. TypeScript/React task
5. Python task
6. Shell/devops task
7. Long-context repo question
8. Refactor with behavior preservation
9. Patch review / risk analysis
10. Dependency or config troubleshooting

Run a matrix:

```powershell
python services/gateway/tools/coding_model_eval.py --model coder --profile coding_fast --attempts 3 --stream
python services/gateway/tools/coding_model_eval.py --model coder --profile coding_repo --attempts 3 --stream
python services/gateway/tools/coding_model_eval.py --model coder --profile coding_reasoning --attempts 2 --stream
python services/gateway/tools/coding_model_eval.py --model coder --profile greedy_probe --attempts 3 --stream
```

JSONL schema fields include:

```json
{
  "timestamp": "ISO-8601 UTC",
  "suite": "suite name",
  "task_id": "task id",
  "task_class": "one of the ten task classes",
  "model": "gateway model or alias",
  "quantization": "operator supplied",
  "runtime": "mlx/vllm/ollama/llama.cpp",
  "host": "stackrot/ai2/ada2",
  "context_length": 32768,
  "temperature": 0.2,
  "top_p": 0.85,
  "top_k": 40,
  "min_p": 0.03,
  "repetition_penalty": 1.03,
  "max_tokens": 8192,
  "prompt_template": "direct_patch_v1",
  "repo_context_method": "all/selected/manifest",
  "tool_loop_count": 0,
  "files_read": ["path"],
  "files_modified": ["path"],
  "test_command": ["python", "tests/test_x.py"],
  "test_result": {"ok": true, "returncode": 0},
  "lint_result": null,
  "pass": true,
  "patch_size": 12,
  "unnecessary_edits": [],
  "hallucinated_paths": [],
  "truncation_events": [],
  "tokens_sec": 30.5,
  "time_to_first_token_ms": 1200.0,
  "wall_clock_ms": 15000.0,
  "notes": []
}
```

## Final Recommendation

Use `local_mlx:mlx-community/Qwen3.6-27B-4bit` as the default `coder` backend
now, with the enforced validation/diff agent loop and larger tool context caps.
The best target setup is Qwen3-Coder-30B-A3B-Instruct-FP8 on `ada2` vLLM with
32K context first, prefix caching enabled, single-agent concurrency, and media
services kept out of the GPU during coding windows. Promote that target to
`coding_repo` only after the eval harness shows stable pass rates, minimal
patches, no hallucinated paths/symbols, and acceptable TTFT across repeated
runs.
