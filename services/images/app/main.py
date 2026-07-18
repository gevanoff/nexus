from __future__ import annotations

from typing import Any, Dict

from app.openai_images_shim import app
from app.openai_images_edits import router as images_edits_router


app.include_router(images_edits_router)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    return {
        "name": "images",
        "version": "0.2",
        "endpoints": {
            "health": "/health",
            "readyz": "/readyz",
            "images_generations": "/v1/images/generations",
            "images_edits": "/v1/images/edits",
        },
        "image_edit_purposes": [
            "image_to_image",
            "composition",
            "style",
            "controlnet",
        ],
        "notes": (
            "OpenAI Images shim for InvokeAI. Text-to-image and purpose-specific "
            "reference-image workflows are supported. Default SHIM_MODE=stub."
        ),
    }
