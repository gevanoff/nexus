# Topology Manifests

These files are the desired-state source of truth for multi-host Nexus placement.

Use topology manifests for:

- which host owns which components
- which host-specific env overrides should be materialized
- which host is expected to run native services outside Compose
- which SSH target and checkout path are canonical for a host profile
- which host platform groups should drive per-hosttype defaults

Do not treat etcd as the deployment plan. etcd is the live runtime registry:

- service registrars publish health-checked endpoints into etcd
- the gateway reads etcd to discover currently available backends
- etcd should reflect deployed state, not replace the versioned plan

Current tracked topology:

- `production.json`: canonical placement for `stackrot`, `ai2`, `ada2`, `meltdown`, `migraine`, and `copyfail`
- `migraine` retains its Hermes Gateway / Telegram role and also owns one tightly bounded host-native MLX lane for a 3B 4-bit model.

Typical workflow:

1. Materialize a host env file from the topology manifest.
2. Deploy that host with `deploy.sh --topology-host <host> ...`.
3. Optionally deploy the same host remotely with `remote-deploy.sh --topology-host <host> ...`.
4. Let service registrars populate etcd from the deployed services.

## Routine Backend Moves

Use the helper when a backend family needs to move between tracked hosts:

```bash
./deploy/scripts/reassign-topology-family.sh --family vllm --from ai2 --to ada2 --write
./deploy/scripts/reassign-topology-family.sh --family tts --from stackrot --to ai2 --write
./deploy/scripts/reassign-topology-family.sh --family qwen3-tts --from stackrot --to ai2 --components-mode ignore --write
```

Supported families today:

- `vllm`
- `tts`
- `qwen3-tts`

Recommended rollout order after a topology move:

1. Deploy the destination host first.
2. Deploy any gateway host next so backend URLs refresh in rendered env files.
3. Deploy the source host last so old components are removed.
4. Verify Gateway and upstream health, run `./deploy/scripts/smoke-test-video.sh` when video backends moved, then re-register services if needed.

Compatibility note:

- The current vLLM deploy path can be assigned either as the monolithic `vllm` profile or as split lanes: `vllm-strong`, `vllm-fast`, `vllm-embeddings`, and the dedicated `vllm-meltdown` Cinder lane.
- All vLLM profiles are GPU-bound (`docker-compose.vllm*.yml` uses `gpus: all`), so they should only be assigned to GPU-capable hosts.
- The tracked `vllm` defaults may require Hugging Face auth or higher rate limits, so set `HUGGING_FACE_HUB_TOKEN` on the destination host when needed.
- `stackrot` has two RTX 3090 24GB GPUs. Treat it as two separate 24GB VRAM lanes, not as one large-memory device.
- `ada2` has 128GB system RAM and a 48GB RTX 6000 Ada. Use the RAM for vLLM CPU offload and startup headroom, but continue to schedule CUDA services by VRAM pressure.
- `ada2` keeps Nexus-owned runtime/control state under `/data/nexus-runtime`; this tree is distinct from backend-owned data roots. Migrated backends use explicit bind-source settings for `/data/invokeai`, `/data/skyreels-v2`, `/data/heartmula`, `/data/followyourcanvas`, and `/data/personaplex`. The shared Hugging Face cache uses `/data/huggingface`.
- The `ada2` topology therefore sets `NEXUS_RUNTIME_ROOT=/data/nexus-runtime` separately from the `*_DATA_BIND_SOURCE` settings and `HF_HOME_BIND_SOURCE=/data/huggingface`. Host compatibility symlinks under `/var/lib` may point at the direct `/data` directories, but Compose bind sources must use the canonical paths.
- Before deploying `ada2` after a storage migration, verify that each selected backend's direct bind source contains its existing state. Docker creates a missing bind-source directory, so a successful container start alone does not prove that models or state were migrated.
- `meltdown` has Ubuntu 22.04, about 47GB system RAM, and a 16GB RTX 5060 Ti. It owns SDXL-Turbo, the lightweight vLLM embeddings lane, and a dedicated unquantized Qwen2.5 3B chat lane for Cinder. The chat lane is capped at an 8K context and 58% GPU-memory utilization so it can coexist with the other services. It uses vLLM's Triton V1 attention backend and disables the FlashInfer sampler because the default Flash Attention path stalled generation on this RTX 5060 Ti.
- `copyfail` has Ubuntu 22.04, an Intel Celeron J3355, 2 logical CPUs, and about 7.4GiB system RAM. It is an infrastructure-only host for metrics collection, deployment orchestration, the shared Honcho memory stack, and general IT operations; do not assign model-serving backends to it. Honcho inference remains on Nexus model hosts through Gateway.
- `migraine` has macOS on Apple M2 with 8GB unified memory. Keep its existing Hermes identity authoritative and limit model serving to the approved host-native 3B 4-bit MLX lane with one concurrent request. Do not assign Compose or vLLM lanes to it.

## vLLM Tool Calling

vLLM automatic tool parsing is enabled only for production chat lanes whose model, chat template, and parser combinations have been validated end to end:

- strong lane (`ada2`): auto tool parsing is enabled with vLLM's `xlam` parser and `/vllm-workspace/examples/tool_chat_template_mistral_parallel.jinja`. This lane backs tools-capable aliases such as `fast-reasoning`.
- fast lane (`stackrot`): Devstral uses the `mistral_serial` profile with vLLM's `mistral` parser and `mistral` tokenizer mode. The Mistral tokenizer supplies its own chat template, so vLLM ignores `--chat-template`; parallel calls are not advertised because live qualification returned an empty structured call array.

The gateway capability flags (`*_NATIVE_TOOLS_ENABLED`) represent validated automatic tool parsing and must match the corresponding vLLM process flags. Otherwise `/v1/chat/completions` requests with `tool_choice=auto` may be passed to a backend that is not actually returning structured tool calls. `deploy/config/vllm-tool-profiles.json` records both sides of that contract, and preflight rejects drift for any lane with a `VLLM*_TOOL_PROFILE` selector.

The production vLLM chat lanes use safetensors rather than GGUF artifacts: `cyankiwi/Devstral-Small-2507-AWQ-4bit` on `stackrot`, `ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic` on `ada2`, and `Qwen/Qwen2.5-3B-Instruct` on `meltdown`. The Meltdown lane is intentionally tool-free and dedicated to Cinder through the `cinder-chat` alias.

The strong vLLM chat lane is configured for a 65536-token context because Hermes Agent rejects models advertised below 64000 tokens. The RTX 6000 Ada lane measured capacity for 88704 KV-cache tokens at its production GPU allocation, so one 64K request fits, but concurrency at the full window is limited. The single-RTX-3090 fast lane remains at 8192: a 65536-token canary required 10.00 GiB of KV cache while only 6.55-7.37 GiB was available, for a measured ceiling of roughly 43000-48000 tokens. CPU weight offload did not free enough VRAM for that KV cache, and assigning its second GPU would displace the Qwen3 TTS lane.

After restarting a lane with automatic native tool flags, validate it directly before flipping the gateway flag:

```bash
BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
REPEATS=10 BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
SMOKE_CASES=auto,required,named,none,roundtrip REPEATS=10 BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
```

The default smoke set deliberately includes parallel calling so qualification discovers rather than assumes that capability. Use the profile's declared case list for its required pass gate; record failures from optional cases in the profile metadata.

For a new vLLM model, select a syntax-family profile explicitly, render the lane settings, and copy the matching Gateway alias fragment:

```bash
./deploy/scripts/vllm-tool-profile.py list
./deploy/scripts/vllm-tool-profile.py render-env --profile mistral_serial --prefix VLLM_FAST
./deploy/scripts/vllm-tool-profile.py alias-json --profile mistral_serial \
  --backend local_vllm_fast --model org/model --context-window 8192
./deploy/scripts/vllm-tool-profile.py check-env --env-file .env
```

A profile is reusable; qualification is model/checkpoint-specific. Parser availability, the emitted syntax, quantization, chat template, and vLLM version can all change results. Keep the alias disabled until the profile's direct cases pass ten times and Gateway's streaming and round-trip qualification also pass. Test unsupported cases too, but advertise only the capabilities that pass.

## ai2 Colima Backend Proxies

The ai2 gateway runs in Colima. On this host, the Colima VM may be able to reach ai2 host services while failing to connect directly to the remote LAN model backends. The production ai2 topology therefore points gateway vLLM URLs at loopback host proxies reachable from containers through `host.docker.internal`:

- `VLLM_FAST_BASE_URL=http://host.docker.internal:18001/v1` forwards to `stackrot:8001`
- `VLLM_EMBEDDINGS_BASE_URL=http://host.docker.internal:18002/v1` forwards to `meltdown:8002`
- `VLLM_BASE_URL=http://host.docker.internal:18003/v1` forwards to `ada2:8003`

Install or refresh the host-side launchd proxy on ai2 before restarting gateway:

```bash
./deploy/scripts/install-backend-port-proxy-launchd.sh --user ai
./deploy/scripts/deploy.sh --topology-host ai2 --components gateway prod main
```

## Image Interfaces

InvokeAI remains useful in production when Nexus needs a managed creative image workspace: model manager, gallery/canvas, and an operator UI behind the OpenAI images shim. Production advertises `INVOKEAI_UI_URL` so the Gateway Image UI can link operators to the InvokeAI interface for model management.

Browser-facing plain-HTTP URLs that use a known short Nexus host alias (for
example `http://ada2:9090`) are resolved by Gateway before they are returned to
the UI. This bridges the current gap between container `extra_hosts` and
operator workstations without changing internal backend routing. Public hostnames
and HTTPS URLs are left unchanged; real DNS remains the preferred long-term
browser path.

ComfyUI should not be added as another always-on CUDA service on `ada2` while `vllm-strong` and InvokeAI/images are resident; the RTX 6000 Ada is the primary contention point. Prefer one of these rollout shapes:

- persistent ComfyUI interface on `ai2`, host-native Apple Silicon, for workflow editing and light/local runs
- on-demand ComfyUI worker on `ada2` for CUDA-only or high-throughput workflows, lifecycle-managed so it trades out conflicting heavy backends

Do not run ComfyUI in Docker on `ai2` for GPU work; macOS GPU access should be host-native.
