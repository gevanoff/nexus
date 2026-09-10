# Coding Agent Task

Goal:

Integrate the HuggingFace model mlx-community/GLM-5.3-mixed-4_5bit into Nexus as a chat backend. Use the generated workspace scaffold. Runtime strategy: mlx. Recommended deployment target: ai2 / MLX. Containerize the adapter if appropriate (no). Provide an industry-standard API surface compatible with OpenAI-style chat access. Update env, compose or host-native launch files, backend registration snippets, implementation stubs, and focused documentation so the workspace is ready for Nexus integration. Preserve existing repository documentation; do not replace a root README wholesale.

Additional user guidance:
Please update our current coder model with this GLM-5.3-mixed-4_5bit

Required deterministic sequence:
1. Parse the requested model reference.
2. Inspect registry metadata and relevant config files.
3. Confirm route/runtime classification and confidence.
4. Confirm size and resource estimates.
5. Confirm host and backend lane placement.
6. Select existing lane, host-native MLX, service shim, or manual-review strategy.
7. Maintain integration/model-integration-dossier.json as durable memory.
8. Apply focused repository changes.
9. Add activation documentation and smoke tests.
10. Register the Resources UI activation candidate.
11. Register the relevant modality catalog entry.
12. Run static tests.
13. Review the diff.
14. Call coding_finish only when the repository is coherently integration-ready.
Do not repeatedly re-read unchanged metadata or diffs; use the dossier as durable memory. Do not download large weights or start the generated service.

Constraints:

1. Reuse the generated scaffold instead of replacing it wholesale.
2. Keep the backend API compatible with `/v1/chat/completions`.
3. Preserve existing repository files, especially the root README; use focused patches for existing docs.
4. If runtime is `mlx`, keep the integration host-native and do not add Docker/Compose as the primary runtime path.
5. If runtime is not `mlx`, provide a containerized path and keep the health/model metadata endpoints consistent with Nexus patterns.
6. Update `integration/backend-config-snippet.yaml` and `integration/lifecycle.backend.json` so operators can wire the backend into Nexus.
7. Document blockers for gated weights, unsupported architectures, or missing runtime features in focused integration docs.
