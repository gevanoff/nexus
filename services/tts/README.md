# Text-to-Speech Service

Text-to-speech synthesis service providing OpenAI-compatible audio generation API.

## Overview

This service provides text-to-speech synthesis capabilities. It exposes an OpenAI-compatible `/v1/audio/speech` endpoint.

## Status

🚧 **Planned** - This service is planned but not yet implemented.

## Planned Features

- **Multiple Voices**: Various voice options and languages
- **OpenAI Compatible**: Drop-in replacement for OpenAI TTS API
- **Streaming**: Real-time audio streaming
- **Multiple Formats**: MP3, WAV, OGG, FLAC support
- **Voice Cloning**: Optional voice cloning capabilities
- **Speed Control**: Adjustable speech rate

## Planned Configuration

```yaml
tts:
  build:
    context: ./services/tts
  ports:
    - "9940:9940"
  environment:
    - BACKEND=piper
    - VOICES_PATH=/data/voices
    - OUTPUT_PATH=/data/outputs
  volumes:
    - tts_data:/data
```

## Planned API

### Generate Speech

```bash
curl -X POST http://localhost:8800/v1/audio/speech \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "input": "Hello, this is a test of the text to speech system.",
    "voice": "nova",
    "model": "tts-1",
    "response_format": "mp3",
    "speed": 1.0
  }' \
  --output speech.mp3
```

Response: Audio file (binary)

## Planned Backends

### Piper
- **Pros**: Fast, lightweight, good quality
- **Cons**: Limited voice selection
- **Resource Usage**: Low (CPU-only)

### Coqui TTS
- **Pros**: High quality, voice cloning
- **Cons**: Heavier resource usage
- **Resource Usage**: Medium (GPU optional)

### Bark
- **Pros**: Very natural, emotion control
- **Cons**: Slower generation
- **Resource Usage**: High (GPU recommended)

## Planned Voices

### Standard Voices
- `alloy` - Neutral, balanced
- `echo` - Male, clear
- `fable` - Female, expressive
- `onyx` - Male, deep
- `nova` - Female, warm
- `shimmer` - Female, soft

### Languages
- English (US, UK, Australian)
- Spanish
- French
- German
- Chinese
- Japanese
- And more...

## Planned Features

### Voice Parameters
```json
{
  "voice": "nova",
  "speed": 1.0,        // 0.25 to 4.0
  "pitch": 0,          // -10 to 10
  "volume": 1.0,       // 0.0 to 1.0
  "language": "en-US"
}
```

### Streaming Response
```bash
curl -X POST http://localhost:8800/v1/audio/speech \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "input": "Long text that should be streamed...",
    "voice": "nova",
    "stream": true
  }' \
  --no-buffer --output -
```

## Implementation TODO

- [ ] Create Dockerfile
- [ ] Implement FastAPI wrapper
- [ ] Add TTS backend integration (Piper, Coqui, or Bark)
- [ ] Add health and metadata endpoints
- [ ] Add voice management
- [ ] Add audio format conversion
- [ ] Add streaming support
- [ ] Add voice cloning (optional)
- [ ] Add SSML support (optional)
- [ ] Add usage metrics

## Requirements

When implemented, this service will require:

- CPU: 2+ cores
- RAM: 4GB+ (8GB+ for voice cloning)
- Disk: 10GB+ for voice models
- Optional: GPU for faster generation

## Audio Formats

### Supported Formats
- **MP3**: Default, widely compatible
- **WAV**: Uncompressed, highest quality
- **OGG**: Good compression, open format
- **FLAC**: Lossless compression
- **AAC**: Modern, efficient

### Sample Rates
- 16kHz - Phone quality
- 22kHz - Standard quality
- 44.1kHz - CD quality
- 48kHz - Professional quality

## Use Cases

- **Audiobook Generation**: Convert books to audio
- **Accessibility**: Screen readers, assistive technology
- **Voice Assistants**: Conversational interfaces
- **Content Creation**: Narration for videos
- **Announcements**: Public announcements, notifications
- **Language Learning**: Pronunciation examples

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
