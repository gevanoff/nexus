# GLM-5.2 context profiles

Nexus exposes the resident `mlx-community/GLM-5.2-4bit` model through distinct aliases instead of forcing chat and autonomous coding to share one budget.

| Alias | Role | Context | Input budget | Compaction | Output | Thinking |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `glm-chat`, `mlx`, `ai2-chat` | Basic chat | 32,768 | 26,000 estimated tokens | n/a | 4,096 | disabled |
| `coder` | Coding Workspace/debugging | 131,072 | 100,000 | 90,000 | 16,384 | enabled |
| `reasoning` | Architecture/difficult debugging | 131,072 | 100,000 | 90,000 | 16,384 | enabled |
| `long` | Maximum-headroom long-horizon work | 131,072 | 104,000 | 94,000 | 24,576 | enabled |

`max_input_tokens` is the hard request budget; `coding_context_reset_tokens` is the soft-compaction point. Durable controller state and working memory survive compaction.

Gateway uses a conservative dependency-free estimate because its process cannot assume the huge tokenizer is mounted: ASCII code/JSON is charged at three characters per token and non-ASCII text at 1.5. Direct, unprofiled GLM IDs retain the legacy `MLX_GLM_MAX_INPUT_CHARS` fallback.

Coding Workspaces count serialized messages and native tool schemas. The old 64,000-character threshold remains only for unprofiled models. Alias output caps also allow the GLM coding and long routes to exceed the global 8,192-token fallback.

Context, output, and thinking policies are granted only by an explicitly selected alias. A raw upstream model ID does not inherit the `long` alias output allowance, and client-supplied `enable_thinking` values cannot override the selected profile. Alias-to-model matching is case-insensitive but still requires the configured backend and model identity to agree.

Restart Gateway after deployment so the alias cache reloads. These aliases share the same resident model, so no model download is required. Verify the deployed `/v1/models` metadata and run one request through `glm-chat`, `coder`, and `long` to confirm that each alias reports the intended context policy and thinking mode.

The final profile values must remain bounded by the context window verified for the deployed MLX runtime.


The deployed resident MLX lane is currently guaranteed at 131,072 tokens. The `long` profile therefore uses the largest verified runtime window with more input/output headroom than `coder`; it does not advertise 256K until a live MLX runtime profile and smoke test validate that size. Oversized `glm-chat` or `coder` requests that still fit the matching `long` budget inherit the long input, output, and thinking policy atomically.
