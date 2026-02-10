# Nexus Replication Plan

This document summarizes what needs to be built to replicate the functional state of the `gateway` and `ai-infra` repositories while applying the new containerized, multi-host approach.

## Required Capabilities

### Core Routing and Auth
- Full OpenAI-compatible gateway endpoints (chat, completions, embeddings, images, audio).
- Multi-tenant auth, token policies, rate limiting, and request auditing.
- Service routing rules that can target specific hosts, GPUs, or model tiers.

### Service Discovery
- Etcd-backed registry for service base URLs and metadata endpoints.
- Automatic health checking and readiness gating per backend.
- Support for remote host discovery without hardcoded hostnames in git.

### Observability
- Structured logs with correlation IDs across gateway and services.
- Metrics endpoints and dashboards (Prometheus + Grafana).
- Tracing hooks for multi-host request spans.

### Agent Runtime
- Tool bus support (as in `gateway`) with agent specs and policy enforcement.
- Memory system (v1/v2) with persistence and isolation per user or workspace.
- Multi-agent orchestration (Adversary and Supplicant roles).

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
5. **Agent runtime**: tool bus, memory system, and multi-agent coordination.
6. **UI container**: separate UI service that consumes `/v1/metadata` for dynamic forms.

## Open Decisions

- Standardize on a single service registry (etcd today, evaluate Consul later).
- Define the UI deployment strategy for multi-host clusters (separate container recommended).
- Decide on network overlay (WireGuard/Tailscale/VPC) for cross-host service traffic.
