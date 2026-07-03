# MLX Service

Host-native MLX OpenAI-compatible service integration for Nexus.

## Placement Policy

- MLX must run host-native on macOS bare metal for Apple Silicon acceleration.
- CPU-only backends that do not benefit from NVIDIA acceleration should run as containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated workloads should run on Linux/NVIDIA hosts.

## Current Host Profile Guidance (verified 2026-05-21)

- `ai2` (Mac M3 Ultra, macOS 15.6, 512GB unified memory): primary host for host-native `mlx` and the Apple Silicon reasoning/coding path.
- `ai1` (Ubuntu Linux, Intel Core i7-12700F, about 46 GiB observed system RAM, 2x GeForce RTX 3090 24GB): secondary Linux/NVIDIA node suitable for `vllm`, embeddings, and overflow CUDA workloads when the topology assigns them there.
- `ada2` (Ubuntu Linux, 13th Gen Intel Core i7-13700K, about 125 GiB observed system RAM, RTX 6000 Ada 48GB class / 46 GiB reported VRAM): primary Linux/NVIDIA node for the heaviest CUDA workloads and the largest `vllm`/image/video profiles.

Use this split to avoid cross-host contention: Apple Silicon-native `mlx` on `ai2`, Linux/NVIDIA `vllm` and CUDA workloads on `ai1`/`ada2`, and exact live placement tracked in `deploy/topology/production.json`.

## Platform Compatibility

`mlx-openai-server` requires **macOS on Apple Silicon (M-series)**. Docker containers in Nexus run Linux userspace/kernel semantics, so this component can fail to start and appear in a restart loop on unsupported environments.

If you see restart-loop behavior for `nexus-mlx`, this is usually a runtime/platform mismatch rather than a Gateway routing issue.

## Status

MLX should be treated as a host-native service on `ai2`, not as a regular Docker workload.
Nexus Gateway reaches it over HTTP via `MLX_BASE_URL`.

## Configuration

See `env/mlx.env.example` for primary variables:

- `MLX_PORT` (default `10240`)
- `MLX_MODEL_PATH` (default `mlx-community/GLM-5.2-DQ4plus-q8`)
- `MLX_MODEL_TYPE` (default `lm`)
- `MLX_CONFIG_PATH` (optional; when set, launch MLX in multi-model config mode)
- `XDG_CACHE_HOME` / `HF_HOME` (optional; move MLX/Hugging Face caches to a larger volume)

### Config Mode

Nexus now supports MLX config-mode launch via `MLX_CONFIG_PATH`.

- Single-model mode:
  - uses `MLX_MODEL_PATH` + `MLX_MODEL_TYPE`
- Config mode:
  - uses `MLX_CONFIG_PATH`
  - lets one MLX server expose multiple model ids and types, such as `lm`, `embeddings`, `multimodal`, `image-generation`, `image-edit`, and `whisper`

Example config template:

- `services/mlx/config/config.example.yaml`

Operational note:

- `mlx-openai-server` supports `on_demand: true` for large models in config mode; these models are advertised in `/v1/models` but loaded only when requested.
- The current `ai2` operating profile keeps GLM-5.2 resident to avoid repeated multi-minute reloads for `long` and coding requests, while MiniMax remains available as an on-demand fallback.
- Only one idle on-demand model is kept loaded at a time, so use on-demand for optional 200GB+ MLX models that do not need to stay hot.
- Non-on-demand models are initialized during server startup. Keep at least one small known-good chat model or embeddings model available for health checks.
- Add optional models incrementally and keep a known-good minimal config available for rollback.

Recommended host/runtime path for operators:

- copy the example to `/var/lib/mlx/config/config.yaml` on the MLX host
- set `MLX_CONFIG_PATH=/var/lib/mlx/config/config.yaml` in `/var/lib/mlx/mlx.env`

Optional MLX-native media surfaces now supported by Nexus Gateway:

- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `POST /v1/audio/transcriptions`

To use them, add matching model entries to the MLX config and point Gateway at `local_mlx`.
The provided `services/mlx/config/config.example.yaml` includes commented examples for:

- `image-generation`
- `image-edit`
- `whisper`

Startup troubleshooting notes:

- Warnings like `Class AVFFrameReceiver is implemented in both .../site-packages/av/... and .../site-packages/cv2/...` come from PyAV/OpenCV shipping overlapping macOS video dylibs. They are noisy, but they are not usually the root cause of MLX startup failure.
- A message like `Handler process for '<model>' did not become ready within 300 s` is the important failure signal. That means one configured model did not finish initializing in time, and MLX may exit before binding the HTTP port.
- If you hit that condition, reduce the config to a minimal known-good set first, verify `curl -fsS http://127.0.0.1:10240/v1/models`, then re-add models one by one.
- For very large first-time downloads, set `HF_TOKEN` in `/var/lib/mlx/mlx.env` to avoid Hugging Face anonymous rate limits.
- If prefetch fails with `No space left on device`, move `XDG_CACHE_HOME` and `HF_HOME` in `/var/lib/mlx/mlx.env` to a larger disk, rerun the installer once, then prefetch again.
- Prefetching large model repos before starting launchd is now supported with `services/mlx/scripts/prefetch-models.sh`.
- When `PREFETCH_BEFORE_START=1` is set in `/var/lib/mlx/mlx.env`, the native MLX launcher also runs that prefetch step before every service start, including `deploy/scripts/restart-mlx.sh` and plain `launchctl kickstart` restarts.
- `install-native-macos.sh` wires this in by default and preserves existing extra keys in `/var/lib/mlx/mlx.env` such as `HF_TOKEN`.

## Native usage

Install host-native MLX on macOS with:

```bash
./services/mlx/scripts/install-native-macos.sh --host 127.0.0.1 --port 10240
```

To download model repos before the MLX server starts:

```bash
./services/mlx/scripts/prefetch-models.sh --config /var/lib/mlx/config/config.yaml
```

After installation, the same helper is copied into the native MLX venv:

```bash
sudo -u mlx env MLX_ENV_FILE=/var/lib/mlx/mlx.env MLX_VENV=/var/lib/mlx/env /var/lib/mlx/env/bin/mlx-prefetch-models
```

`install-native-macos.sh` installs both the wrapper and its Python helper into `/var/lib/mlx/env/bin`, so `deploy/scripts/restart-mlx.sh` and plain `launchctl kickstart` can use the same prefetch mechanism without relying on the repo checkout at runtime.

`install-native-macos.sh` also applies Nexus' `mlx-openai-server` compatibility patch after pip installation. The current patch keeps DeepSeek MLX generation on the handler thread because that model family binds some MLX GPU stream state to the thread that creates it. `deploy/scripts/restart-mlx.sh` reapplies the patch idempotently before launchd restarts so a future package reinstall does not silently lose the fix.

## Notes

- Gateway containers on the same Mac should use `MLX_BASE_URL=http://host.docker.internal:10240/v1`.
- Remote Gateway hosts should use the MLX host IP/DNS name instead.
- MLX model/runtime compatibility depends on host-native environment and chosen model.

After first install, the native launchd job reads runtime settings from `/var/lib/mlx/mlx.env`.
To change models later, update that file and restart the service without rewriting the plist:

```bash
sudo sed -i '' 's#^MLX_MODEL_PATH=.*#MLX_MODEL_PATH=mlx-community/GLM-5.2-DQ4plus-q8#' /var/lib/mlx/mlx.env
./deploy/scripts/restart-mlx.sh
```

You can also change `MLX_MODEL_TYPE`, `MLX_HOST`, and `MLX_PORT` in the same file.
If `MLX_CONFIG_PATH` is set in `/var/lib/mlx/mlx.env`, the launcher uses config mode instead.
`PREFETCH_BEFORE_START=1` tells the native launcher to prefetch model repos before each service start, including `deploy/scripts/restart-mlx.sh` and `launchctl kickstart` restarts.
If local system storage is too small for model caches, set `XDG_CACHE_HOME` and `HF_HOME` to a larger mounted volume before rerunning the installer.

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
- `prewarm-mlx.sh` assumes the MLX HTTP server is already up. Use `prefetch-models.sh` first when a new config prevents MLX from binding its port.

Gateway integration pattern:

- Run MLX host-native on Apple Silicon (`127.0.0.1:10240/v1` on the MLX host).
- Set `MLX_BASE_URL` in `nexus/.env` to the host URL that Gateway containers can reach (for same-machine Docker Desktop, `http://host.docker.internal:10240/v1`).
- Gateway uses this backend for chat/embeddings when routing selects backend class `local_mlx`.
- Gateway can also proxy MLX image generation, image editing, and Whisper transcription when those model types are present in the MLX config.
- Multimodal chat requests are passed through when message content uses structured OpenAI-style arrays/objects.

## Recommended Model Strategy for `ai2` (512GB)

With 512GB unified memory, `ai2` can run very large MLX models. The current large-model profile keeps GLM-5.2 resident because it backs the `mlx`, `coder`, and `long` aliases and has expensive reload latency. MiniMax stays configured as the on-demand fallback. Other heavy MLX profiles should stay on-demand unless there is a specific reason to keep them hot:

- `mlx`: `mlx-community/GLM-5.2-DQ4plus-q8`
- `reasoning`: `mlx-community/DeepSeek-R1-0528-4bit`
- `coder`: `mlx-community/GLM-5.2-DQ4plus-q8`
- `minimax candidate`: `mlx-community/MiniMax-M3-4bit` requires `mlx-vlm>=0.6.3`, which is not compatible with `mlx-openai-server==1.8.1`; keep it out of live routing until a compatible server release is available
- `phi-4-reasoning-plus`: `mlx-community/Phi-4-reasoning-plus-4bit` as the smaller reasoning fallback and lightweight chat health model
- `long`: `mlx-community/GLM-5.2-DQ4plus-q8` with raised `context_window`

Recommended `ai2` alias-to-model mapping:

```json
{
	"aliases": {
		"default": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"tools": true
		},
		"mlx": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-DQ4plus-q8",
			"tools": true
		},
		"coder": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-DQ4plus-q8",
			"tools": true
		},
		"reasoning": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"context_window": 2048,
			"tools": true,
			"max_tokens_cap": 512
		},
		"fast-reasoning": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"context_window": 2048,
			"tools": true,
			"max_tokens_cap": 256
		},
		"long": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-DQ4plus-q8",
			"context_window": 65536,
			"tools": true
		}
	}
}
```

Operational note for `ai2`:

- Legacy `mlx-coder` references are mapped to `local_mlx` in Gateway so stale aliases do not appear as a separate stopped backend class.
- Configure GLM-5.2 with `on_demand: false` so it stays resident. Keep MiniMax on-demand as the fallback; first request loads it, later requests reuse it until the idle timeout.
- For text/code use, configure these models with `model_type: lm`; reserve `model_type: multimodal` for MLX-VLM converted repos. Validate with `curl -fsS http://127.0.0.1:10240/v1/models` after restart.
- GLM-5.2 uses the GLM `<tool_call><arg_key>...` chat-template shape, so configure it with the `glm4_moe` tool and reasoning parsers. MiniMax uses custom model code, so its fallback config should set `trust_remote_code: true`.

## Are these models already configured?

Yes, in the current packaged Gateway alias file and MLX config example.

- The packaged aliases live in `services/gateway/app/model_aliases.json`.
- Runtime Gateway aliases still live in `nexus/.runtime/gateway/config/model_aliases.json`; refresh or deploy Gateway after changing the packaged file.
- Runtime MLX model serving still depends on `/var/lib/mlx/config/config.yaml` on the MLX host.

## Do you need to prewarm?

Yes—after changing aliases or restarting services, prewarm the selected runtime/model set.

- Prewarm MLX aliases/models:

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/GLM-5.2-DQ4plus-q8
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/MiniMax-M3-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/DeepSeek-R1-0528-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/Phi-4-reasoning-plus-4bit
```

- Prewarm remote Ollama checker models:

```bash
./deploy/scripts/prewarm-models.sh --external-ollama --model <your-coder-model>
```

If models are not already present locally, first-request warmup may trigger a download/conversion step and take significantly longer.

Why keep Ollama alongside MLX (even on `ai2`):

- Ollama gives broader one-command model availability and easier fallback coverage.
- MLX gives top Apple Silicon efficiency and excellent low-latency local inference.
- Running both lets Gateway keep MLX as the primary local path while still using remote Ollama models as independent checks or fallback reviewers.

Then restart Gateway so aliases are reloaded:

```bash
docker-compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --build gateway
```

Usage via OpenAI-compatible API with the example alias profiles above:

- `model: "fast"` routes to the low-latency alias tier
- `model: "default"` routes to the primary local MLX reasoning model
- `model: "coder"` routes to the primary local MLX coding path
- `model: "long"` routes to the MLX long-context profile

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
