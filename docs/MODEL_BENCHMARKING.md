# Model Benchmarking

Use Model Admin to benchmark Nexus OpenAI-compatible chat models and aliases for
completion tokens per second.

Model Admin records one JSONL object per run and shows a compact summary table.
Streaming mode is used so the benchmark can also report time to first token and
decode throughput.

## Quick Start

1. Open `/ui/admin/models`.
2. Select aliases or concrete backend models in the Benchmark panel.
3. Set `Max tokens`, `Runs`, and `Warmups`.
4. Click `Start benchmark`.

Results are written inside the gateway to:

```text
/var/lib/gateway/data/model_benchmarks/results.jsonl
```

With the standard compose mount, the host-side path is:

```text
${NEXUS_RUNTIME_ROOT}/gateway/data/model_benchmarks/results.jsonl
```

## Useful Options

- Select simple aliases such as `fast`, `default`, or `coder` to measure the
  same route users select in Chat.
- Select concrete backend/model rows such as `local_mlx:<model>` when you need a
  targeted backend comparison.
- `Max tokens` controls the requested completion length.
- `Runs` controls measured runs per model.
- `Warmups` runs uncounted requests before measured runs.
- `MODEL_BENCHMARK_LOG_PATH` overrides the JSONL result path.

For automation outside the UI, `services/gateway/tools/model_benchmark.py` can
run the same style of OpenAI-compatible benchmark against a gateway URL. Its
local default output path is `.runtime/model-benchmarks/results.jsonl`.

## Metrics

- `tokens_per_sec`: completion tokens divided by full wall-clock request time.
- `decode_tokens_per_sec`: completion tokens divided by wall time after the
  first streamed content chunk. This is only available for streaming runs.
- `time_to_first_token_ms`: elapsed time until the first streamed content chunk.
- `completion_tokens_source`: `usage` when the backend returned
  `usage.completion_tokens`; otherwise `estimated`.

For apples-to-apples comparisons, keep prompt, `max_tokens`, sampling settings,
gateway load, and backend lifecycle state fixed across model runs.
