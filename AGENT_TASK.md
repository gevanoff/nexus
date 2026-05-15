# Coding Agent Task

Goal:

Integrate the HuggingFace model nvidia/NVIDIA-Nemotron-Nano-9B-v2 into Nexus as a chat backend. Use the generated workspace scaffold. Runtime strategy: transformers. Recommended deployment target: ai1 / vLLM Fast. Containerize the adapter if appropriate (yes). Provide an industry-standard API surface compatible with OpenAI-style chat access. Update README, env, compose or host-native launch files, backend registration snippets, and the implementation stub so the workspace is ready for Nexus integration.

Constraints:

1. Reuse the generated scaffold instead of replacing it wholesale.
2. Keep the backend API compatible with `/v1/chat/completions`.
3. If runtime is `mlx`, keep the integration host-native and do not add Docker/Compose as the primary runtime path.
4. If runtime is not `mlx`, provide a containerized path and keep the health/model metadata endpoints consistent with Nexus patterns.
5. Update `integration/backend-config-snippet.yaml` and `integration/lifecycle.backend.json` so operators can wire the backend into Nexus.
6. Document blockers for gated weights, unsupported architectures, or missing runtime features in the README.
