# TTS Service

OpenAI-compatible text-to-speech backend.

## Overview

This service is implemented by porting the **Pocket TTS FastAPI shim** from `ai-infra/services/pocket-tts`.

It exposes an OpenAI-compatible `POST /v1/audio/speech` endpoint that the gateway can proxy.

## Status

✅ Implemented (Pocket TTS shim)

## Endpoints

- `GET /health`
- `GET /readyz`
- `GET /v1/models`
- `POST /v1/audio/speech`
- `GET /v1/metadata`

## Configuration

- Env template: `env/pocket-tts.env.example`
- Primary knobs:
  - `POCKET_TTS_BACKEND=auto|python|command`
  - `POCKET_TTS_COMMAND=pocket-tts`
  - `POCKET_TTS_MODEL_PATH=`
  - `POCKET_TTS_VOICE=alba`

## Quick test

```bash
curl -sS http://localhost:9940/health
curl -sS http://localhost:9940/v1/models

curl -sS -X POST http://localhost:9940/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"hello","voice":"alba","response_format":"wav"}' \
  --output out.wav
```

## Docker Compose (current)

Nexus persists TTS state on the host under `nexus/.runtime/tts/` and bind-mounts it into the container.

```yaml
tts:
  build:
    context: ./services/tts
    dockerfile: Dockerfile
  ports:
    - "9940:9940"
  environment:
    - POCKET_TTS_HOST=0.0.0.0
    - POCKET_TTS_PORT=9940
    - POCKET_TTS_BACKEND=${POCKET_TTS_BACKEND:-auto}
  volumes:
    - ./.runtime/tts/data:/data
```

## Contributing

Want to implement this service? See:
- [Template Service](../template/README.md)
- [SERVICE_API_SPECIFICATION.md](../../SERVICE_API_SPECIFICATION.md)
- [Piper TTS](https://github.com/rhasspy/piper)
- [Coqui TTS](https://github.com/coqui-ai/TTS)

## Example Implementation

Minimal Python/FastAPI implementation:

```python
from fastapi import FastAPI
from pydantic import BaseModel
import subprocess

app = FastAPI()

class SpeechRequest(BaseModel):
    input: str
    voice: str = "nova"
    response_format: str = "mp3"

@app.post("/v1/audio/speech")
async def create_speech(request: SpeechRequest):
    # Generate audio using TTS engine
    audio_data = generate_audio(
        text=request.input,
        voice=request.voice,
        format=request.response_format
    )
    
    return Response(
        content=audio_data,
        media_type=f"audio/{request.response_format}"
    )
```

## References

- [OpenAI Audio API](https://platform.openai.com/docs/api-reference/audio)
- [Piper TTS](https://github.com/rhasspy/piper)
- [Coqui TTS](https://github.com/coqui-ai/TTS)
- [Bark](https://github.com/suno-ai/bark)
- [XTTS](https://huggingface.co/coqui/XTTS-v2)
