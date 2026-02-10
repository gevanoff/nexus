# Nexus Ideas & Open Questions

This document captures early-stage ideas, open questions, and decisions to validate. As items are resolved, they should be consolidated into the main documentation and removed from here.

## Consolidated Into Documentation

- **Container-first architecture, standardized APIs, and metadata-driven discovery** are documented in `ARCHITECTURE.md` and `SERVICE_API_SPECIFICATION.md`.
- **Deployment, migration, and operational guidance** are documented in `docs/DEPLOYMENT.md` and `docs/MIGRATION.md`.

## Desired Feature Set (High Level)

- **Gateway-centric API surface** with OpenAI-compatible endpoints and standardized service discovery.
- **Template-driven service creation**: new services should be easy to scaffold and integrate via `/v1/metadata`.
- **Observability-first**: consistent health checks, metrics, and structured logs.
- **Agentic runtime**: multiple agents, including Adversary and Supplicant roles, with strict governance.
- **Fast recovery**: automated rollback or restart if a service fails.

## Shortest Bootstrapping Path

1. **Single-host baseline**: run gateway + one inference backend locally using Docker Compose.
2. **Metadata contract**: ensure `/v1/metadata`, `/health`, and `/readyz` are correct for the first backend.
3. **Gateway routing**: confirm OpenAI-compatible chat requests succeed through the gateway.
4. **Minimal observability**: enable metrics and basic logs; verify health checks.
5. **Scale out**: move one service to a second host and update gateway configuration.

## Distributed Containers: Open Questions

- **Service discovery evolution**: etcd is the default registry; decide if Consul or another system is preferred long-term.
- **Network overlay**: WireGuard/Tailscale vs. cloud VPC for multi-host traffic?
- **Trust boundary**: do we require mTLS between gateway and backends, or is a private network enough?
- **Routing policy**: should the gateway support per-host routing rules (GPU type, capacity, location)?
- **Service metadata ownership**: which fields are required for scheduler/routing (e.g., GPU memory)?

## Likely Stumbling Blocks

- **Cross-host networking**: DNS, firewalling, and latency for multi-host services.
- **GPU scheduling**: mapping workloads to the right GPU without hardcoding host identities.
- **Credential distribution**: secrets for registry, TLS, or mTLS across hosts.
- **Cold start times**: large model downloads on new hosts.
- **Logging at scale**: correlating requests across multiple hosts.

## Next Questions to Answer

- Which registry/coordination system should Nexus use (if any) for cross-host discovery?
- What is the minimal mTLS story for internal APIs?
- What is the preferred config format for mapping remote service endpoints (env vs. registry)?
- What is the first "real" service to build after gateway + Ollama?

## Action Items

- Document multi-host networking patterns and recommended bootstrapping flow.
- Define the minimal gateway configuration for remote backends.
- Outline a security hardening checklist (mTLS, firewall rules, secrets storage).
