from __future__ import annotations

from typing import Any, Dict

from app.openai_images_shim import app


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    # Minimal metadata for Nexus service discovery.
    return {
        "name": "images",
        "version": "0.1",
        "endpoints": {
            "health": "/health",
            "readyz": "/readyz",
            "images_generations": "/v1/images/generations",
        },
        "notes": "OpenAI Images shim (InvokeAI-compatible). Default SHIM_MODE=stub.",
    }
