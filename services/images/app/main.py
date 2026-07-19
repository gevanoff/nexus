from __future__ import annotations

from typing import Any, Dict

from app import openai_images_shim
from app.model_compat import install_model_compat
from app.workflow_routing import install_workflow_routing


install_model_compat(openai_images_shim)
install_workflow_routing(openai_images_shim)
app = openai_images_shim.app

from app.openai_images_edits import router as images_edits_router  # noqa: E402


app.include_router(images_edits_router)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    return {
        "name": "images",
        "version": "0.3",
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
            "OpenAI Images shim for InvokeAI. Text-to-image generation automatically selects a configured "
            "workflow by the selected type='main' model's family. Purpose-specific reference-image workflows "
            "are supported separately. Model and workflow mismatches return actionable errors. Default "
            "SHIM_MODE=stub."
        ),
    }
