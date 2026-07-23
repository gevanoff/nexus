# Dynamic Backend UI Model

This document defines how Nexus discovers OpenAI-ish service backends and turns their descriptors/status into UI surfaces. The current UI is gateway-served and uses both descriptor-driven data and hand-built focused views.

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
- `luxtts` and `qwen3-tts` (audio/speech)
- `local_mlx` (chat/models and, depending on MLX config, image/edit/transcription models)
- `heartmula` (music generation through gateway UI/API)
- `lighton-ocr` (OCR/scan)
- `ltx-video`, `hunyuan-video`, and `followyourcanvas` (video generation)
- `personaplex` (chat and live UI proxying)

## Descriptor Contract

Backends should expose:

- `/v1/metadata` (required)
- `/v1/descriptor` (recommended)

`/v1/descriptor` extends metadata with:

- `response_types`: expected media type(s) by mode (e.g., JSON vs SSE)
- `ui_navigation`: placement hints (`primary`, `side-panel`, group labels)
- `ui.options`: structured controls used to render specialized forms

## Gateway Interpretation

The current gateway:

1. Discovers services from etcd/env records.
2. Uses backend config, registry records, lifecycle metadata, health probes, model aliases, and focused UI helper APIs to compose UI state.
3. Builds `GET /ui/api/backend_status` for the Chat/Resources status panels, including lifecycle-manager state, host/resource metrics, gateway readiness, aliases, stale-status hints, and non-model core components such as Telegram bot.
4. Exposes focused UI helper APIs, such as `/ui/api/image/catalog`, `/ui/api/tts/backends`, `/ui/api/music/backends`, `/ui/api/video/backends`, `/ui/api/ocr/backends`, and `/ui/api/models`.

Planned/generated UI work may add public descriptor catalog endpoints later, but `GET /v1/backends/catalog` and `GET /v1/ui/layout` are not current gateway routes.

## UI Organization Strategy

- **Primary front-end**: Chat (`/ui`, backed by `/ui/api/chat_stream` and `/v1/chat/completions`).
- **Focused UIs**: `/ui/image`, `/ui/music`, `/ui/video`, `/ui/ocr`, `/ui/tts`, `/ui/voice-clone`, `/ui/personaplex`, `/ui/coding`, `/ui/tasks`, and `/ui/resources`.
- **Resources UI**: canonical backend/resource page. It groups backends by type, includes host/resource status, supports lifecycle activation/deactivation where available, and is linked from backend status boxes elsewhere in the UI.
- **Chat backend status**: compact overview only. Detailed host info and lifecycle controls belong in Resources to avoid cluttering Chat.
- **Per-panel controls**: implemented by focused UI code today, with descriptor `ui.options` remaining the contract for future generated controls.

This keeps a single top-level Chat UX while allowing capability-specific interfaces for image/audio/tool backends.
