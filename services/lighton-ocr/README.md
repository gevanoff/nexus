# LightOnOCR Service

Containerized LightOnOCR shim exposing `POST /v1/ocr`.

This port preserves both proxy mode and subprocess mode from `ai-infra`, but the default Nexus container is self-contained and uses the bundled `run_lighton_ocr.py` helper.

## Runtime

- Recommended host: `ada2`
- Possible host: `ai1` if VRAM is sufficient
- Default port: `9155`
- The container is ready at idle without holding persistent VRAM; model load occurs inside the request subprocess.

## Gateway integration

Set `LIGHTON_OCR_API_BASE_URL` to the reachable service URL, for example `http://ada2:9155`.

## Production Smoke Test

Verified on `ada2` with:

```json
{
  "image_url": "https://dummyimage.com/800x240/ffffff/000000.png&text=HELLO+NEXUS+OCR",
  "prompt": "Read all visible text exactly."
}
```

The service returned `HELLO NEXUS OCR` using the `lighton-native` path on CUDA.
