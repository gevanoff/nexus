# Nexus Replication Plan

This document summarizes the remaining parity gaps after the current Nexus
gateway migration. It is no longer a from-zero build plan: the gateway, UI,
agent runtime, coding workspace, tools bus, lifecycle manager, and several
backend shims are implemented and deployed through the Nexus repo.

## Required Capabilities

### Core Routing and Auth
- Implemented: OpenAI-compatible gateway endpoints for chat, completions, embeddings, responses, rerank, images, audio speech, transcription, and music.
- Implemented: UI users, browser sessions, per-user API keys, static bearer fallback, and token policy hooks.
- Remaining: stronger rate limiting/audit reporting and richer resource-aware routing that can reason over host/GPU pressure before dispatch.

### Service Discovery
- Implemented: etcd-backed registry, env/static config, topology rendering, backend health checks, Resources UI, and lifecycle-manager integration.
- Remaining: deeper artifact/preflight checks before activation and richer lifecycle state for memory/VRAM/startup failure causes.

### Observability
- Implemented: gateway health/readiness, Prometheus metrics endpoint, backend status/resource UI, request IDs, and agent/coding run logs.
- Remaining: central dashboards, distributed tracing, and unified log correlation across all remote hosts.

### Agent Runtime
- Implemented: tool bus with tier/allowlist enforcement, agent specs, run persistence, web browsing, memory tools, scheduled-task tools, and multi-backend coordinator.
- Implemented: coding workspaces with autonomous runs, workspace steering messages, checkpoint commits, push/PR actions, and per-user GitHub token/preferred model settings.
- Implemented: scheduled LLM tasks with timer/run-at/interval/cron schedules and a focused UI.
- Remaining: scheduled coder tasks, app/multi-model scheduled runners, and more formal role/team orchestration beyond the current coordinator.

### Backend Services
- LLM inference (Ollama, MLX) with streaming support.
- Image generation service (InvokeAI/Comfy) with metadata and model catalogs.
- Audio/TTS service with streaming audio response support.
- OCR/video generation services (as needed for parity).

### Deployment & Safety
- Container security hardening (non-root, restricted capabilities).
- mTLS for internal traffic on shared networks.
- Backups and rollback workflows for persistent data volumes.
- Branch-based deployment workflow (dev → main) with environment-specific configs.

## Suggested Build Order

1. **Gateway parity**: complete OpenAI-compatible endpoints + auth + routing.
2. **Service discovery**: etcd registration and health monitoring across hosts.
3. **Core backends**: Ollama + one image + one TTS service with metadata endpoints.
4. **Observability**: metrics, logs, and dashboards across all services.
5. **Scheduled/coding automation**: extend the current LLM scheduled-task runner to coder, app, multi-model, image, music, and video tasks.
6. **UI split decision**: keep the gateway-served UI as the supported path unless scale/security requirements justify a separate UI service.

## Open Decisions

- Standardize on a single service registry (etcd today, evaluate Consul later).
- Define whether/when the gateway-served UI should split into a separate service.
- Decide on network overlay (WireGuard/Tailscale/VPC) for cross-host service traffic.
- Decide whether coding GitHub auth should stay per-user PAT based or move to GitHub App installation tokens.
