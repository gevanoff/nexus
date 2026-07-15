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

## Agent And Scheduled Task Expansion

- Extend the current scheduled LLM task runner to support coder workspaces, including branch selection, checkpoint commits, and draft PR creation policies.
- Add scheduled task runners for app workflows, multi-model review/synthesis, image generation, music generation, and video generation.
- Add a Resources/Admin view for scheduled task queue health, missed runs, retry policy, and stale/failed runner diagnostics.
- Decide whether scheduled tasks should be able to activate lifecycle-managed backends before execution and deactivate them after completion.

## Honcho And Host Chatbots

- Provision dedicated Honcho database, JWT, and restricted Gateway credentials on `copyfail`, then start the pinned stack in `docker-compose.honcho.yml`.
- Implement and test the canonical owner, private/group partition, cross-bot sharing, retention, deletion, and export rules in `deploy/honcho/MEMORY_POLICY.md` before Telegram messages are uploaded.
- Add Honcho SDK integration to each chatbot only after download authorization and deletion-driven re-derivation are enforced; use workspace-scoped JWTs, not the Honcho admin token.
- Select and benchmark the future `meltdown` language model, including VRAM coexistence with SDXL-Turbo and the embeddings lane.
- After the `meltdown` model is chosen, add its Gateway alias, SOUL, distinct BotFather token, Compose service, lifecycle status entry, and end-to-end chat health probe. Do not create a placeholder route that can appear ready before a model exists.
