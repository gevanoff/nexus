# Host SOUL.md Identities

Nexus supports version-controlled, host-specific conversational identities for explicit model aliases. The design follows the current Hermes Agent personality model: `SOUL.md` is durable identity and voice, while operational instructions remain in `AGENTS.md` or service configuration.

Primary references:

- [Hermes Agent: Personality and SOUL.md](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/personality.md)
- [OpenClaw SOUL.md template](https://github.com/openclaw/openclaw/blob/main/docs/reference/templates/SOUL.md)

## Prompt behavior

An alias can select a SOUL by name:

```json
{
  "backend": "local_vllm",
  "model": "example/model",
  "soul": "ada2"
}
```

Gateway reads `NEXUS_SOUL_ROOT/<name>/SOUL.md`, validates the name and content, caps it at `NEXUS_SOUL_MAX_CHARS`, and inserts the content directly as the first system message. Existing client system messages remain after the identity. The same request is not injected twice during Gateway tool loops.

SOUL selection comes only from operator-controlled aliases. Clients cannot provide filesystem paths. Files containing model role/control tokens are rejected, missing files are logged, and an invalid SOUL never blocks ordinary aliases.

## Separation of concerns

Use SOUL.md for stable identity, tone, communication defaults, disagreement, and uncertainty. Do not put ports, credentials, deployment commands, tool instructions, temporary tasks, or repository conventions in it.

The current Nexus-owned identities are:

- `souls/ai2/SOUL.md` through `ai2-chat`
- `souls/ada2/SOUL.md` through `ada2-chat`
- `souls/stackrot/SOUL.md` through `stackrot-chat`
- `souls/meltdown/SOUL.md` through `cinder-chat` (currently backed by the shared fast Devstral lane)

`migraine` is different: its existing Hermes instance owns `~/.hermes/SOUL.md`. Nexus does not duplicate or overwrite it.
