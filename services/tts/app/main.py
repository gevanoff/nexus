from __future__ import annotations

from typing import Any, Dict

from app.pocket_tts_server import app


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    return {
        "name": "tts",
        "version": "0.1",
        "endpoints": {
            "health": "/health",
            "readyz": "/readyz",
            "models": "/v1/models",
            "audio_speech": "/v1/audio/speech",
        },
        "notes": "Pocket TTS shim (ported from ai-infra).",
    }
