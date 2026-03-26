# MLX Service

Host-native MLX OpenAI-compatible service integration for Nexus.

## Placement Policy

- MLX must run host-native on macOS bare metal for Apple Silicon acceleration.
- CPU-only backends that do not benefit from NVIDIA acceleration should run as containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated workloads should run on Linux/NVIDIA hosts.

## Current Host Profile Guidance (2026-03-02)

- `ai2` (macOS Apple Silicon, 512GB unified memory): primary host for host-native `mlx` and `ollama`.
- `ada2` (Linux, RTX 6000 Ada 46GB VRAM, ~31GiB RAM): keep focused on CUDA-heavy workloads (currently occupied by `heartmula` + `invokeai`).
- `ai1` (Linux, RTX 5060 Ti 16GB VRAM, ~15GiB RAM): keep focused on smaller CUDA image workloads (currently SDXL Turbo active).

Use this split to avoid cross-host contention: LLM chat/coding/default aliases on `ai2`, image/CUDA pipelines on `ada2`/`ai1`.

## Platform Compatibility

`mlx-openai-server` requires **macOS on Apple Silicon (M-series)**. Docker containers in Nexus run Linux userspace/kernel semantics, so this component can fail to start and appear in a restart loop on unsupported environments.

If you see restart-loop behavior for `nexus-mlx`, this is usually a runtime/platform mismatch rather than a Gateway routing issue.

## Status

MLX should be treated as a host-native service on `ai2`, not as a regular Docker workload.
Nexus Gateway reaches it over HTTP via `MLX_BASE_URL`.

## Configuration

See `env/mlx.env.example` for primary variables:

- `MLX_PORT` (default `10240`)
- `MLX_MODEL_PATH` (default `mlx-community/gemma-2-2b-it-8bit`)
- `MLX_MODEL_TYPE` (default `lm`)
- `MLX_CONFIG_PATH` (optional; when set, launch MLX in multi-model config mode)

### Config Mode

Nexus now supports MLX config-mode launch via `MLX_CONFIG_PATH`.

- Single-model mode:
  - uses `MLX_MODEL_PATH` + `MLX_MODEL_TYPE`
- Config mode:
  - uses `MLX_CONFIG_PATH`
  - lets one MLX server expose multiple model ids and types, such as `lm`, `embeddings`, and `multimodal`

Example config template:

- `services/mlx/config/config.example.yaml`

Recommended host/runtime path for operators:

- copy the example to `/var/lib/mlx/config/config.yaml` on the MLX host
- set `MLX_CONFIG_PATH=/var/lib/mlx/config/config.yaml` in `/var/lib/mlx/mlx.env`

## Native usage

Install host-native MLX on macOS with:

```bash
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

## Notes

- Gateway containers on the same Mac should use `MLX_BASE_URL=http://host.docker.internal:10240/v1`.
- Remote Gateway hosts should use the MLX host IP/DNS name instead.
- MLX model/runtime compatibility depends on host-native environment and chosen model.

After first install, the native launchd job reads runtime settings from `/var/lib/mlx/mlx.env`.
To change models later, update that file and restart the service without rewriting the plist:

```bash
sudo sed -i '' 's#^MLX_MODEL_PATH=.*#MLX_MODEL_PATH=mlx-community/Qwen2.5-32B-Instruct-4bit#' /var/lib/mlx/mlx.env
sudo launchctl kickstart -k system/com.nexus.mlx.openai.server
```

You can also change `MLX_MODEL_TYPE`, `MLX_HOST`, and `MLX_PORT` in the same file.
If `MLX_CONFIG_PATH` is set in `/var/lib/mlx/mlx.env`, the launcher uses config mode instead.

Installer prerequisites:

- Python `>=3.11` is required for current `mlx-openai-server` builds.
- If your default `python3` is older (for example macOS system Python 3.9), install a newer one and pin it for install:

```bash
MLX_PYTHON=/opt/homebrew/bin/python3.12 ./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

Prewarm MLX runtime/model (recommended after install or restart):

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1
```

For very large first-time model downloads/warmups, keep timeout disabled (default):

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --timeout-sec 0
```

Prewarm from Gateway alias config (`.runtime/gateway/config/model_aliases.json`):

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --from-aliases
./deploy/scripts/prewarm-models.sh --external-ollama --from-aliases
```

Notes:

- `prewarm-mlx.sh --from-aliases` warms every unique alias with `"backend": "mlx"` or `"backend": "local_mlx"`.
- `prewarm-models.sh --from-aliases` checks/pulls every unique alias with `"backend": "ollama"`.
- Both scripts also support `--aliases-file <path>` to point at a non-default alias file.
- `prewarm-mlx.sh` uses `--timeout-sec 0` by default (no timeout), which is recommended for large model first-run warmups.
- When `MLX_CONFIG_PATH` is set, use `--model` and/or `--from-aliases` with `prewarm-mlx.sh` for deterministic warmup.

Gateway integration pattern:

- Run MLX host-native on Apple Silicon (`127.0.0.1:10240/v1` on the MLX host).
- Set `MLX_BASE_URL` in `nexus/.env` to the host URL that Gateway containers can reach (for same-machine Docker Desktop, `http://host.docker.internal:10240/v1`).
- Gateway uses this backend for chat/embeddings when routing selects backend class `local_mlx`.
- Multimodal chat requests are passed through when message content uses structured OpenAI-style arrays/objects.

## Recommended Model Strategy for `ai2` (512GB)

With 512GB unified memory, `ai2` can run much larger MLX models than typical Mac deployments. Best-practice routing is still tiered by latency target:

- `fast` (interactive/lowest latency): 7B–14B instruct models on MLX.
- `default` (best quality for general chat): 32B–72B instruct models (MLX or Ollama, choose by measured latency/quality).
- `coder` (tool-heavy/codegen): coding-specialized 14B–32B model, usually Ollama first for broader catalog.
- `long` (large context sessions): MLX model profile with raised `context_window` and conservative concurrency.

Recommended `ai2` alias-to-model mapping (starting point):

```json
{
	"aliases": {
		"fast": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-7B-Instruct-4bit",
			"tools": false
		},
		"default": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-32B-Instruct-4bit",
			"tools": true
		},
		"coder": {
			"backend": "ollama",
			"model": "qwen2.5-coder:32b",
			"tools": true
		},
		"long": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-14B-Instruct-4bit",
			"context_window": 65536,
			"tools": false
		}
	}
}
```

`ai2` quality-max profile (higher latency, higher quality):

```json
{
	"aliases": {
		"fast": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-14B-Instruct-4bit",
			"tools": false
		},
		"default": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-72B-Instruct-4bit",
			"tools": true
		},
		"coder": {
			"backend": "ollama",
			"model": "qwen2.5-coder:32b",
			"tools": true
		},
		"long": {
			"backend": "mlx",
			"model": "mlx-community/Qwen2.5-32B-Instruct-4bit",
			"context_window": 131072,
			"tools": false
		}
	}
}
```

Alias-by-alias alternatives (if available and validated in your environment):

- `fast` (lowest latency):
	- Primary: `mlx-community/Qwen2.5-7B-Instruct-4bit`
	- Alternatives: `mlx-community/Llama-3.1-8B-Instruct-4bit`, `mlx-community/Gemma-2-9B-it-4bit`
- `default` (best overall quality):
	- Primary: `mlx-community/Qwen2.5-32B-Instruct-4bit`
	- Alternatives: `mlx-community/Qwen2.5-72B-Instruct-4bit` (higher quality, higher latency), `mlx-community/Llama-3.3-70B-Instruct-4bit`
- `coder` (code + tools):
	- Primary: `qwen2.5-coder:32b` (Ollama)
	- MLX candidate if preferred: `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit` or `mlx-community/Qwen2.5-Coder-32B-Instruct-4bit`
- `long` (extended context):
	- Primary: `mlx-community/Qwen2.5-14B-Instruct-4bit` with `context_window` `65536`
	- Alternatives: use the same family as `default` with reduced concurrency, or lower-parameter instruct model for higher sustained throughput.

If a specific MLX model identifier is unavailable, keep alias names and routing shape, then swap only `model` values.

## Are these models already configured?

Not by default in a fresh checkout.

- These model mappings are documentation examples until you place one profile into `nexus/.runtime/gateway/config/model_aliases.json` on the host running Gateway.
- In the current workspace, that runtime file does not exist yet, so these aliases are not active.

## Do you need to prewarm?

Yes—after changing aliases or restarting services, prewarm the selected runtime/model set.

- Prewarm MLX aliases/models:

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/Qwen2.5-14B-Instruct-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/Qwen2.5-32B-Instruct-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/Qwen2.5-72B-Instruct-4bit
```

- Prewarm Ollama aliases/models:

```bash
./deploy/scripts/prewarm-models.sh --external-ollama --model qwen2.5-coder:32b
```

If models are not already present locally, first-request warmup may trigger a download/conversion step and take significantly longer.

Why keep Ollama alongside MLX (even on `ai2`):

- Ollama gives broader one-command model availability and easier fallback coverage.
- MLX gives top Apple Silicon efficiency and excellent low-latency local inference.
- Running both lets Gateway route by workload instead of forcing one runtime for all requests.

Then restart Gateway so aliases are reloaded:

```bash
docker-compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --build gateway
```

Usage via OpenAI-compatible API:

- `model: "fast"` routes to MLX (low-latency interactive prompts)
- `model: "default"` routes to Ollama strong model
- `model: "coder"` routes to Ollama coding model
- `model: "long"` routes to MLX long-context profile

## Native MLX Checklist

1. Run MLX natively on an Apple Silicon macOS host.
2. Point Gateway at that host by setting `MLX_BASE_URL` in `nexus/.env`.
3. Use `--external-mlx` for Gateway verification/diagnostics.

Example:

```bash
MLX_BASE_URL=http://<mac-host-or-ip>:10240/v1
```

Native MLX quick path:

```bash
# 1) Install/start native MLX on macOS
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240

# 2) Verify local health on macOS host
curl -fsS http://127.0.0.1:10240/v1/models

# 3) Update nexus/.env
# MLX_BASE_URL=http://host.docker.internal:10240/v1

# 4) Start Nexus without mlx container
docker compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml up -d --build

# 5) Verify gateway contract using external/native MLX
./deploy/scripts/verify-gateway.sh --external-mlx
```

`docker-compose.mlx.yml` remains in the repo as a legacy scaffold, but it is not the recommended path for `ai2`.

## Security Baseline (Native MLX Host)

- Run MLX under a dedicated non-admin service account.
- Prefer loopback-only binding and publish externally only through a constrained reverse proxy.
- Restrict ingress to Gateway/control-plane source IPs with host firewall rules.
- Keep model/cache paths owned by the service account with least-privilege permissions.
