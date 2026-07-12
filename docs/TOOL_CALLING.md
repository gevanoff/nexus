# Nexus Tool Calling

Nexus exposes a provider-neutral tool layer through `POST /v1/chat/completions`. MLX and vLLM models emit OpenAI-compatible function calls; Gateway either returns those calls to the client or executes approved Nexus tools and continues the model loop.

## Execution Modes

Select a mode with `x_nexus.tool_execution_mode`:

```json
{
  "model": "default",
  "messages": [{"role": "user", "content": "Check Nexus health."}],
  "x_nexus": {
    "tool_execution_mode": "gateway_exec",
    "toolsets": ["core", "repo", "ops"],
    "max_tool_rounds": 4
  }
}
```

- `gateway_exec`: inject approved tools, execute model tool calls, append tool results, and continue until a final answer.
- `client_exec`: preserve OpenAI tool calls for Continue or another client to execute. This is the conservative server default.
- `disabled`: reject tool fields.

`NEXUS_TOOL_EXECUTION_DEFAULT` sets the server default. Nexus-owned callers should explicitly request `gateway_exec`; external clients should use `client_exec`. Client tools use the `replace`, `merge`, or `client` policy selected by `x_nexus.client_tools` or `NEXUS_CLIENT_TOOL_POLICY`.

With `stream=true`, Gateway buffers internal model/tool rounds and emits only the final assistant response as valid Chat Completions SSE followed by `[DONE]`.

`/v1/responses` preserves its existing non-tool behavior but returns a clear 400 error for `gateway_exec`; use Chat Completions for Gateway-side execution.

## Built-In Toolsets

- `core`: `nexus_health`, `nexus_models_list`, `nexus_alias_resolve`, `nexus_tool_diagnostics`
- `repo`: `nexus_file_list`, `nexus_file_read`, `nexus_file_grep`, `nexus_git_status`, `nexus_git_diff`
- `ops`: `nexus_resources_snapshot`, `nexus_docker_ps`, `nexus_docker_logs`, `nexus_service_status`, `nexus_http_request`
- `write_ops`: write/restart tools, disabled by default
- `shell`: shell and Python execution, disabled by default

All built-in schemas are strict OpenAI function schemas: object parameters, every property required, nullable optional values represented with `null`, `additionalProperties=false`, and `strict=true`.

## Security

Gateway execution is bounded by toolset and per-tool enablement, path/host/container/service allowlists, per-tool and whole-loop timeouts, output limits, parallel-call limits, maximum rounds, secret redaction, and an audit JSONL. No arbitrary shell, filesystem write, restart, commit, or patch operation is enabled by default.

The Gateway container mounts the Nexus checkout read-only at `/workspace/nexus`. Docker tools return a structured unavailable result unless an operator explicitly supplies a safe Docker control mechanism; Nexus does not mount the Docker socket by default.

Relevant settings:

```bash
NEXUS_TOOL_EXECUTION_DEFAULT=client_exec
NEXUS_AUTO_INJECT_TOOLS=false
NEXUS_AUTO_INJECT_TOOLSETS=core,repo,ops
NEXUS_CLIENT_TOOL_POLICY=replace
NEXUS_TOOL_MAX_ROUNDS=4
NEXUS_TOOL_MAX_PARALLEL=4
NEXUS_TOOL_TIMEOUT_SEC=20
NEXUS_TOOL_LOOP_TIMEOUT_SEC=120
NEXUS_TOOL_OUTPUT_MAX_CHARS=12000
NEXUS_TOOL_FS_ROOTS=/workspace/nexus,/var/lib/gateway/app,/var/lib/gateway/config
```

## Provider Configuration

vLLM automatic tool choice requires `--enable-auto-tool-choice`, a validated `--tool-call-parser`, and any model-specific `--chat-template`. Nexus renders these from `VLLM_ENABLE_AUTO_TOOL_CHOICE`, `VLLM_TOOL_CALL_PARSER`, and `VLLM_CHAT_TEMPLATE`. Required and named choices may use vLLM guided decoding even when automatic parsing is disabled.

Native MLX capability is model-specific. Configure the served model parser, for example:

```yaml
tool_call_parser: glm4_moe
reasoning_parser: glm4_moe
```

Do not mark an alias tool-capable until its parser passes named, required, auto, parallel, continuation, and streaming probes. Alias diagnostics are available at `GET /v1/tool-calling/diagnostics`.

## Verification

Run unit tests:

```bash
python -m pytest services/gateway/tests -q
```

Run live Gateway execution smoke tests without printing the token:

```bash
python services/gateway/tools/smoke_gateway_exec_tools.py \
  --base-url http://ai2:8800/v1 \
  --token "$GATEWAY_BEARER_TOKEN" \
  --models default,coder,long \
  --toolsets core,repo,ops
```

The script emits one JSON object per model/scenario and exits nonzero on failure. Tool audit records are written to `/var/lib/gateway/data/tools/gateway_exec.jsonl`; arguments are omitted and secret-like values are redacted.
