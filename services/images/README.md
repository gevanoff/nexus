# Image Generation Service

Text-to-image generation service providing OpenAI-compatible image generation API.

## Overview

This service provides text-to-image generation capabilities using state-of-the-art models like SDXL. It exposes an OpenAI-compatible `/v1/images/generations` endpoint.

## Status

🚧 **Planned** - This service is planned but not yet implemented.

## Planned Features

- **SDXL Support**: High-quality image generation with Stable Diffusion XL
- **Multiple Backends**: InvokeAI, ComfyUI, or Automatic1111
- **OpenAI Compatible**: Drop-in replacement for OpenAI DALL-E API
- **GPU Accelerated**: NVIDIA GPU support for fast generation
- **Multiple Sizes**: Support for various image dimensions
- **Style Controls**: Various artistic styles and parameters

## Planned Configuration

```yaml
images:
  build:
    context: ./services/images
  ports:
    - "7860:7860"
  environment:
    - BACKEND=invokeai
    - MODELS_PATH=/data/models
    - OUTPUT_PATH=/data/outputs
  volumes:
    - images_data:/data
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

## Planned API

### Generate Image

```bash
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "prompt": "A serene mountain landscape at sunset",
    "model": "sdxl-base-1.0",
    "size": "1024x1024",
    "n": 1,
    "response_format": "url"
  }'
```

Response:
```json
{
  "created": 1234567890,
  "data": [
    {
      "url": "http://localhost:8800/images/abc123.png"
    }
  ]
}
```

## Planned Backends

### InvokeAI
- **Pros**: Feature-rich, web UI, model management
- **Cons**: Heavier resource usage
- **GPU Memory**: 8GB+ recommended

### ComfyUI
- **Pros**: Node-based workflow, very flexible
- **Cons**: More complex setup
- **GPU Memory**: 8GB+ recommended

### Automatic1111
- **Pros**: Popular, well-documented, extensions
- **Cons**: Less modern API
- **GPU Memory**: 6GB+ recommended

## Planned Models

- `sdxl-base-1.0` - Stable Diffusion XL base model
- `sdxl-turbo` - Fast SDXL variant
- `sd-1.5` - Stable Diffusion 1.5 (lighter weight)

## Implementation TODO

- [ ] Create Dockerfile
- [ ] Implement FastAPI wrapper
- [ ] Add InvokeAI integration
- [ ] Add health and metadata endpoints
- [ ] Add image storage and retrieval
- [ ] Add model management
- [ ] Add style presets
- [ ] Add safety filters
- [ ] Add rate limiting
- [ ] Add usage metrics

## Requirements

When implemented, this service will require:

- NVIDIA GPU with 8GB+ VRAM
- CUDA 11.8+
- Docker with NVIDIA runtime
- 50GB+ disk space for models

## Contributing

Want to implement this service? See:
- [Template Service](../template/README.md)
- [SERVICE_API_SPECIFICATION.md](../../SERVICE_API_SPECIFICATION.md)
- [InvokeAI Documentation](https://invoke-ai.github.io/InvokeAI/)

## References

- [OpenAI Images API](https://platform.openai.com/docs/api-reference/images)
- [InvokeAI](https://github.com/invoke-ai/InvokeAI)
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- [Automatic1111](https://github.com/AUTOMATIC1111/stable-diffusion-webui)
- [SDXL](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
