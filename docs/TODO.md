# Nexus To-Do

## Backend Lifecycle And Service Standardization

- Make PersonaPlex upstream UI/runtime a first-class compose/lifecycle component instead of a manual `.runtime/personaplex/app/docker-compose.yaml` launch.
- Add required-secret checks to lifecycle-manager status, especially for gated Hugging Face repos.
- Add model artifact manifest support so lifecycle status can show missing checkpoint files before a user starts a backend.
- Bake heavy upstream dependencies into service images where practical; avoid startup `pip install` for HeartMula-style runtimes.
- Add a backend preflight command that checks host placement, secrets, artifact paths, disk, ports, GPU visibility, health, and one smoke request.
- Store idle and peak observed VRAM in lifecycle metadata after every successful backend bring-up.
- Add host system RAM and swap pressure to lifecycle decisions, especially for vLLM CPU offload and large model startup phases.
- Track observed system RAM, VRAM, and startup exit reasons in lifecycle state so an exited-137 backend is distinguished from a normal user trade-out.
- Benchmark whether ada2's `VLLM_CPU_OFFLOAD_GB=8` should stay conservative or move to a larger value now that the host has 128GB RAM.
- Add a UI path for difficult swaps that shows which active backends would be stopped and why.
- Make gateway disabled-backend configuration lifecycle-aware so a traded-in backend can become visible without manual env edits.
- Add artifact proxy tests for every backend that returns generated media.
- Extend service scaffolding to optionally generate gateway config and lifecycle config patches.
