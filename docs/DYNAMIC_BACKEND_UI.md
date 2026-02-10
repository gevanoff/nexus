# Dynamic Backend UI Model

This document defines how Nexus discovers OpenAI-ish service backends and turns their descriptors into specialized backend UI panels behind Chat.

## Which Backends Are OpenAI-ish?

A backend is considered OpenAI-ish if it exposes one or more of these endpoints:

- `/v1/chat/completions`
- `/v1/completions`
- `/v1/embeddings`
- `/v1/images/generations`
- `/v1/audio/speech`
- `/v1/models`

In Nexus, examples include:

- `ollama` (chat/completions/models)
- `images` (images/generations)
- `tts` (audio/speech)

## Descriptor Contract

Backends should expose:

- `/v1/metadata` (required)
- `/v1/descriptor` (recommended)

`/v1/descriptor` extends metadata with:

- `response_types`: expected media type(s) by mode (e.g., JSON vs SSE)
- `ui_navigation`: placement hints (`primary`, `side-panel`, group labels)
- `ui.options`: structured controls used to render specialized forms

## Gateway Interpretation

The gateway now:

1. Discovers services from etcd/env records.
2. Fetches `/v1/descriptor` for each service (falls back to `/v1/metadata`).
3. Builds `GET /v1/backends/catalog` for clients/UIs.
4. Builds `GET /v1/ui/layout` for UI composition.

## UI Organization Strategy

- **Primary front-end**: Chat (`/v1/chat/completions`)
- **Specialized backend panels**: generated from backend descriptors and shown as side tabs/panels.
- **Per-panel controls**: rendered from `ui.options` and endpoint contracts.

This keeps a single top-level Chat UX while allowing capability-specific interfaces for image/audio/tool backends.
