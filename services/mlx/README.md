# MLX Service

Host-native MLX OpenAI-compatible service integration for Nexus.

## Placement Policy

- MLX must run host-native on macOS bare metal for Apple Silicon acceleration.
- CPU-only backends that do not benefit from NVIDIA acceleration should run as containers on a Mac (currently only `ai2`).
- NVIDIA-accelerated workloads should run on Linux/NVIDIA hosts.

## Current Host Profile Guidance (verified 2026-05-21)

- `ai2` (Mac M3 Ultra, macOS 15.6, 512GB unified memory): primary host for host-native `mlx` and the Apple Silicon reasoning/coding path.
- `stackrot` (Ubuntu Linux, Intel Core i7-12700F, about 46 GiB observed system RAM, 2x GeForce RTX 3090 24GB): secondary Linux/NVIDIA node suitable for `vllm`, embeddings, and overflow CUDA workloads when the topology assigns them there.
- `ada2` (Ubuntu Linux, 13th Gen Intel Core i7-13700K, about 125 GiB observed system RAM, RTX 6000 Ada 48GB class / 46 GiB reported VRAM): primary Linux/NVIDIA node for the heaviest CUDA workloads and the largest `vllm`/image/video profiles.

Use this split to avoid cross-host contention: Apple Silicon-native `mlx` on `ai2`, Linux/NVIDIA `vllm` and CUDA workloads on `stackrot`/`ada2`, and exact live placement tracked in `deploy/topology/production.json`.

## Platform Compatibility

`mlx-openai-server` requires **macOS on Apple Silicon (M-series)**. Docker containers in Nexus run Linux userspace/kernel semantics, so this component can fail to start and appear in a restart loop on unsupported environments.

If you see restart-loop behavior for `nexus-mlx`, this is usually a runtime/platform mismatch rather than a Gateway routing issue.

## Status

MLX should be treated as a host-native service on `ai2`, not as a regular Docker workload.
Nexus Gateway reaches it over HTTP via `MLX_BASE_URL`.

## Configuration

See `env/mlx.env.example` for primary variables:

- `MLX_PORT` (default `10240`)
- `MLX_MODEL_PATH` (default `mlx-community/GLM-5.2-4bit`)
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

- Although `mlx-openai-server` supports `on_demand: true`, do not use it for Huge models. Request-time loading cannot provide a guarded transition or useful service while hundreds of gigabytes are being loaded.
- The current `ai2` operating profile exposes exactly one resident Huge model. Replace it only through the guarded Model Admin operation.
- Keep other Huge models in the cache/admin catalog but out of the live MLX config until an administrator selects one.
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
- A message like `Handler process for '<model>' did not become ready within 900 s` is the important failure signal. Nexus patches the upstream 300-second handler limit and exposes `MLX_MODEL_READY_TIMEOUT_SEC` (default `900`) because very large models can need more than five minutes to initialize.
- If you hit that condition, reduce the config to a minimal known-good set first, verify `curl -fsS http://127.0.0.1:10240/v1/models`, then re-add models one by one.
- For very large first-time downloads, set `HF_TOKEN` in `/var/lib/mlx/mlx.env` to avoid Hugging Face anonymous rate limits.
- If prefetch fails with `No space left on device`, move `XDG_CACHE_HOME` and `HF_HOME` in `/var/lib/mlx/mlx.env` to a larger disk, rerun the installer once, then prefetch again.
- Prefetching large model repos before starting launchd is now supported with `services/mlx/scripts/prefetch-models.sh`.
- Prefetch writes an atomic `.nexus_download_status.json` beside each Hugging Face model cache. Model Admin shows completed/total weight shards, current attempt, retry state, and the latest error while polling active jobs.
- Transient failures resume automatically with bounded exponential backoff. Defaults are five attempts, 30-second initial delay, and a 300-second delay cap; configure `MLX_PREFETCH_MAX_ATTEMPTS`, `MLX_PREFETCH_RETRY_BASE_SEC`, `MLX_PREFETCH_RETRY_MAX_SEC`, and `MLX_PREFETCH_PROGRESS_INTERVAL_SEC` on lifecycle-manager.
- One per-model lock prevents duplicate download workers. A failed job leaves its resumable cache intact; **Restart fetch** resumes it, while **Re-download** explicitly purges it first.
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
sudo sed -i '' 's#^MLX_MODEL_PATH=.*#MLX_MODEL_PATH=mlx-community/GLM-5.2-4bit#' /var/lib/mlx/mlx.env
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
./deploy/scripts/prewarm-vllm.sh
```

Notes:

- `prewarm-mlx.sh --from-aliases` warms every unique alias with `"backend": "mlx"` or `"backend": "local_mlx"`.
- `prewarm-mlx.sh` also supports `--aliases-file <path>` to point at a non-default alias file.
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

With 512GB unified memory, `ai2` can run very large MLX models. The current profile keeps GLM-5.2 resident because it backs the `mlx`, `coder`, and `long` aliases and has expensive reload latency. Other Huge profiles remain cache-only candidates and require an explicit guarded resident switch:

- `mlx`: `mlx-community/GLM-5.2-4bit`
- `reasoning`: `mlx-community/DeepSeek-R1-0528-4bit`
- `coder`: `mlx-community/GLM-5.2-4bit`
- `phi-4-reasoning-plus`: `mlx-community/Phi-4-reasoning-plus-4bit` as the smaller reasoning fallback and lightweight chat health model
- `long`: `mlx-community/GLM-5.2-4bit` with raised `context_window`

Recommended `ai2` alias-to-model mapping:

```json
{
	"aliases": {
		"default": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"context_window": 65536,
			"tools": true
		},
		"mlx": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-4bit",
			"context_window": 65536,
			"tools": true
		},
		"coder": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-4bit",
			"context_window": 65536,
			"tools": true
		},
		"reasoning": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"context_window": 65536,
			"tools": true,
			"max_tokens_cap": 1024
		},
		"fast-reasoning": {
			"backend": "local_vllm",
			"model": "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
			"context_window": 65536,
			"tools": true,
			"max_tokens_cap": 512
		},
		"long": {
			"backend": "local_mlx",
			"model": "mlx-community/GLM-5.2-4bit",
			"context_window": 65536,
			"tools": true
		}
	}
}
```

Operational note for `ai2`:

- The Huge MLX aliases advertise 65536-token contexts. GLM-5.2 and DeepSeek R1 declare 1048576 and 163840 positions respectively; their latent-attention KV dimensions require only several GiB for a 64K fp16 cache, leaving headroom on the 512GB host even with one Huge model resident.
- Legacy `mlx-coder` references are mapped to `local_mlx` in Gateway so stale aliases do not appear as a separate stopped backend class.
- Configure exactly one Huge model with `on_demand: false`. Never advertise another Huge model as on-demand; select replacements through Model Admin so ordinary requests cannot initiate a memory transition.
- For text/code use, configure these models with `model_type: lm`; reserve `model_type: multimodal` for MLX-VLM converted repos. Validate with `curl -fsS http://127.0.0.1:10240/v1/models` after restart.
- GLM-5.2 uses the GLM `<tool_call><arg_key>...` chat-template shape, so configure it with the `glm4_moe` tool and reasoning parsers.
- Gateway-side execution is provider-neutral: MLX emits OpenAI-compatible `tool_calls`, then Gateway executes approved tools and sends `role: tool` results back to MLX. Keep unsupported MLX aliases at `tools: false`; see `docs/TOOL_CALLING.md` and `/v1/tool-calling/diagnostics`.

## Low-Latency GLM-5.2 Notes

For coding-agent workloads, the biggest UX gains come from reducing time-to-first-token (TTFT) and maximizing prompt-prefix reuse:

- Keep `mlx-community/GLM-5.2-4bit` resident (`on_demand: false`) to avoid repeated load penalties.
- Keep `MLX_MODEL_READY_TIMEOUT_SEC=3600` for this model. Its roughly 390 GB cache lives on `/ai-data`, so a guarded cold transition can be storage-bound. The gateway allows 600 seconds for ordinary non-streaming calls and has no streaming read timeout, but interactive requests must only reach MLX after the resident handler is ready.
- Keep system/developer scaffold stable across turns so MLX can reuse longer prompt prefixes.
- Prewarm after restarts before routing interactive traffic.
- Prefer deterministic tool/schema ordering in gateway request construction.

Operator memory tuning on large Apple Silicon hosts can also help stability under very large model pressure.

The guarded resident switch records and applies a model-specific value. On the
512 GB ai2 host, GLM-5.2 4-bit requires the following ceiling:

```bash
sudo sysctl iogpu.wired_limit_mb=450000
```

Caution:

- Treat this as an ai2/model-specific setting, not a portable default.
- Higher wired limits can impact overall system responsiveness and other workloads.
- Validate with your own stability and thermal envelope before making persistent boot-time changes.

## Are these models already configured?

Yes, in the current packaged Gateway alias file and MLX config example.

- The packaged aliases live in `services/gateway/app/model_aliases.json`.
- Runtime Gateway aliases still live in `nexus/.runtime/gateway/config/model_aliases.json`; refresh or deploy Gateway after changing the packaged file.
- Runtime MLX model serving still depends on `/var/lib/mlx/config/config.yaml` on the MLX host.

## Do you need to prewarm?

Yes—after changing aliases or restarting services, prewarm the selected runtime/model set.

- Prewarm MLX aliases/models:

```bash
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/GLM-5.2-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/DeepSeek-R1-0528-4bit
./deploy/scripts/prewarm-mlx.sh --mlx-base-url http://127.0.0.1:10240/v1 --model mlx-community/Phi-4-reasoning-plus-4bit
```

- Prewarm the configured vLLM lanes:

```bash
./deploy/scripts/prewarm-vllm.sh
```

If models are not already present locally, first-request warmup may trigger a download/conversion step and take significantly longer.

The tracked fallback path is vLLM on its assigned NVIDIA hosts. Ollama is not a
production fallback for the current topology.

Then restart Gateway so aliases are reloaded:

```bash
./deploy/scripts/restart-gateway.sh
```

Usage via OpenAI-compatible API with the example alias profiles above:

- `model: "fast"` routes to the low-latency alias tier
- `model: "default"` routes to the primary local MLX reasoning model
- `model: "coder"` routes to the primary local MLX coding path
- `model: "long"` routes to the MLX long-context profile

## Native MLX Checklist

1. Run MLX natively on an Apple Silicon macOS host.
2. Point Gateway at that host by setting `MLX_BASE_URL` in `nexus/.env`.
3. Run `verify-gateway.sh` and `diagnose-gateway.sh`; both inspect the running
   deployment and no longer need provider-placement flags.

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

# 4) Deploy the tracked host topology
./deploy/scripts/deploy.sh --topology-host ai2 prod main

# 5) Verify the running gateway contract
./deploy/scripts/verify-gateway.sh
```

`docker-compose.mlx.yml` remains in the repo as a legacy scaffold, but it is not the recommended path for `ai2`.

## Security Baseline (Native MLX Host)

- Run MLX under a dedicated non-admin service account.
- Prefer loopback-only binding and publish externally only through a constrained reverse proxy.
- Restrict ingress to Gateway/control-plane source IPs with host firewall rules.
- Keep model/cache paths owned by the service account with least-privilege permissions.
