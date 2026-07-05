# GLM-5.2 MLX Latency Optimization Guide

This guide documents the Nexus-side changes and operator workflow for reducing perceived latency and TTFT for GLM-5.2 coding-agent traffic.

## Objectives

- Reduce time-to-first-token (TTFT) for iterative coding requests.
- Preserve OpenAI compatibility while improving determinism.
- Keep changes inspectable through structured telemetry and reproducible benchmark scripts.

## Gateway Changes

The gateway now applies deterministic request shaping and emits structured latency telemetry for both stream and non-stream chat paths.

## Deterministic Request Construction

Gateway canonicalization now:

- normalizes tools payload shape;
- sorts tool definitions deterministically;
- serializes prefix content with stable JSON key ordering;
- computes a prompt-prefix fingerprint hash and length.

This improves observability and increases the chance that semantically equivalent requests hit the same backend prefix-cache behavior.

## Prompt-Prefix Telemetry

Telemetry is controlled by:

- `PROMPT_PREFIX_TELEMETRY_ENABLED` (default: `true`)
- `PROMPT_PREFIX_OBSERVATION_CACHE_SIZE` (default: `2048`)

The gateway logs a structured record for each chat call (`gateway.chat_latency`) containing:

- request and model identity (`request_id`, requested/resolved model, upstream)
- latency (`ttft_ms` when measurable, `total_ms`)
- throughput (`decode_tokens_per_sec`)
- token usage (`prompt_tokens`, `completion_tokens`, `total_tokens` when available)
- prefix reuse hints (`prompt_prefix_hash`, `prompt_prefix_chars`, `estimated_reused_prefix_chars`, `cache_candidate`)

`estimated_reused_prefix_chars` and `cache_candidate` are local observations for comparative analysis; they are not direct backend cache internals.

## Benchmark Workflow

Use:

```bash
python services/gateway/tools/benchmark_glm52_latency.py \
  --base-url http://127.0.0.1:8800/v1 \
  --model glm-5.2 \
  --api-key "$NEXUS_API_KEY"
```

The benchmark emits JSON lines for three scenarios:

1. `cold_long_prompt`
2. `same_prefix_new_user`
3. `continuation_follow_up`

Track these fields across runs:

- `time_to_first_token_ms`
- `total_ms`
- `decode_tokens_per_sec`
- `prompt_prefix_hash`
- `prefix_seen_before`

## Prewarm Strategy

Use stable warm prompts for GLM-5.2 after restart:

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/GLM-5.2-DQ4plus-q8
```

Default warm prompt is deterministic and coding-oriented to keep startup behavior inspectable and closer to real traffic.

## Operator Notes For ai2

For large Apple Silicon memory pressure scenarios, operators sometimes test:

```bash
sudo sysctl iogpu.wired_limit_mb=380000
# or
sudo sysctl iogpu.wired_limit_mb=430000
```

These are situational tuning values, not guaranteed-safe defaults. Validate host responsiveness, thermals, and service stability before adopting persistent boot-time configuration.
