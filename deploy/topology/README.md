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

- `production.json`: canonical placement for `stackrot`, `ai2`, `ada2`, `meltdown`, and `copyfail`
- `migraine` is intentionally not a deploy target in this manifest. It is a client-only Hermes Gateway / Telegram host that consumes Nexus models through the gateway.

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

- The current vLLM deploy path can be assigned either as the monolithic `vllm` profile or as split lanes: `vllm-strong`, `vllm-fast`, and `vllm-embeddings`.
- All vLLM profiles are GPU-bound (`docker-compose.vllm*.yml` uses `gpus: all`), so they should only be assigned to GPU-capable hosts.
- The tracked `vllm` defaults may require Hugging Face auth or higher rate limits, so set `HUGGING_FACE_HUB_TOKEN` on the destination host when needed.
- `stackrot` has two RTX 3090 24GB GPUs. Treat it as two separate 24GB VRAM lanes, not as one large-memory device.
- `ada2` has 128GB system RAM and a 48GB RTX 6000 Ada. Use the RAM for vLLM CPU offload and startup headroom, but continue to schedule CUDA services by VRAM pressure.
- `meltdown` has Ubuntu 22.04, about 47GB system RAM, and a 16GB RTX 5060 Ti. It currently owns SDXL-Turbo and the vLLM embeddings lane; treat it as a lighter CUDA overflow/staging host, not a replacement for `ada2`.
- `copyfail` has Ubuntu 22.04, an Intel Celeron J3355, 2 logical CPUs, and about 7.4GiB system RAM. It is an infrastructure-only host for metrics collection, deployment orchestration, and general IT operations; do not assign model-serving backends to it.
- `migraine` has macOS on Apple M2 with 8GB unified memory. Keep it client-only for Hermes/Telegram; do not assign Compose model-serving backends or vLLM lanes to it.

## vLLM Tool Calling

vLLM automatic tool parsing is enabled only for production chat lanes whose model, chat template, and parser combinations have been validated end to end:

- strong lane (`ada2`): auto tool parsing is enabled with vLLM's `xlam` parser and `/vllm-workspace/examples/tool_chat_template_mistral_parallel.jinja`. This lane backs tools-capable aliases such as `fast-reasoning`.
- fast lane (`stackrot`): auto tool parsing is disabled. vLLM 0.10.2 runs this Devstral lane with the matching tokenizer in `mistral` tokenizer mode, but the lane is not validated for native automatic structured tool-call parsing.

The gateway capability flags (`*_NATIVE_TOOLS_ENABLED`) represent validated automatic tool parsing and must match the corresponding vLLM process flags. Otherwise `/v1/chat/completions` requests with `tool_choice=auto` may be passed to a backend that is not actually returning structured tool calls. Required and named tool choices are still allowed through vLLM because they use guided decoding instead of the automatic parser.

The production vLLM chat lanes use Mistral-family safetensors (`cyankiwi/Devstral-Small-2507-AWQ-4bit` on `stackrot` and `ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic` on `ada2`) rather than GGUF artifacts. The `ada2` model is text-only because the available Mistral3 multimodal Magistral repos either failed vLLM v0.10.2 initialization or produced invalid text in smoke tests. `meltdown` currently serves the vLLM embeddings lane only; there is no chat tool-call surface on that host unless a chat model is assigned there.

The strong vLLM chat lane is configured for a 65536-token context because Hermes Agent rejects models advertised below 64000 tokens. The RTX 6000 Ada lane measured capacity for 88704 KV-cache tokens at its production GPU allocation, so one 64K request fits, but concurrency at the full window is limited. The single-RTX-3090 fast lane remains at 8192: a 65536-token canary required 10.00 GiB of KV cache while only 6.55-7.37 GiB was available, for a measured ceiling of roughly 43000-48000 tokens. CPU weight offload did not free enough VRAM for that KV cache, and assigning its second GPU would displace the Qwen3 TTS lane.

After restarting a lane with automatic native tool flags, validate it directly before flipping the gateway flag:

```bash
BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
REPEATS=10 BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
SMOKE_CASES=required,named,none BASE_URL=http://127.0.0.1:8000/v1 MODEL=<served-model-name> ./deploy/scripts/smoke-vllm-tools.sh
```

## ai2 Colima Backend Proxies

The ai2 gateway runs in Colima. On this host, the Colima VM may be able to reach ai2 host services while failing to connect directly to the remote LAN model backends. The production ai2 topology therefore points gateway vLLM URLs at loopback host proxies reachable from containers through `host.docker.internal`:

- `VLLM_FAST_BASE_URL=http://host.docker.internal:18001/v1` forwards to `stackrot:8001`
- `VLLM_EMBEDDINGS_BASE_URL=http://host.docker.internal:18002/v1` forwards to `meltdown:8002`
- `VLLM_BASE_URL=http://host.docker.internal:18003/v1` forwards to `ada2:8003`

Install or refresh the host-side launchd proxy on ai2 before restarting gateway:

```bash
./deploy/scripts/install-backend-port-proxy-launchd.sh --user ai
./deploy/scripts/deploy.sh --topology-host ai2 --components gateway
```

## Image Interfaces

InvokeAI remains useful in production when Nexus needs a managed creative image workspace: model manager, gallery/canvas, and an operator UI behind the OpenAI images shim. Production advertises `INVOKEAI_UI_URL` so the Gateway Image UI can link operators to the InvokeAI interface for model management.

ComfyUI should not be added as another always-on CUDA service on `ada2` while `vllm-strong` and InvokeAI/images are resident; the RTX 6000 Ada is the primary contention point. Prefer one of these rollout shapes:

- persistent ComfyUI interface on `ai2`, host-native Apple Silicon, for workflow editing and light/local runs
- on-demand ComfyUI worker on `ada2` for CUDA-only or high-throughput workflows, lifecycle-managed so it trades out conflicting heavy backends

Do not run ComfyUI in Docker on `ai2` for GPU work; macOS GPU access should be host-native.
