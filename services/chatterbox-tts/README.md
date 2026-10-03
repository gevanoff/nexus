# Chatterbox Turbo TTS

Local OpenAI-compatible Nexus TTS backend for Resemble AI's Chatterbox-Turbo.

## Endpoints

- `POST /v1/audio/speech`
- `GET /v1/voices`
- `GET /v1/models`
- `GET /v1/metadata`
- `GET /health`
- `GET /readyz`

Reference clips are read from the shared Nexus TTS reference directory and selected by filename stem.
Chatterbox Turbo requires more than five seconds of reference audio for zero-shot cloning.

Supported generation controls:

- `voice`
- `speed` (0.5-2.0, pitch-preserving post-generation time stretch)
- `temperature`
- `top_p`
- `top_k`
- `repetition_penalty`
- `seed`
- `norm_loudness`

Inline Turbo paralinguistic tags such as `[laugh]`, `[chuckle]`, and `[cough]` are passed through in the input text.

Turbo does not support the original Chatterbox `cfg_weight`, `exaggeration`, or `min_p` controls; Nexus deliberately does not advertise them.
