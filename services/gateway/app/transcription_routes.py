from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.auth import require_bearer
from app.backends import check_capability, get_admission_controller
from app.config import S
from app.health_checker import check_backend_ready
from app.upstreams import transcribe_openai_audio


router = APIRouter()


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(req: Request):
    require_bearer(req)
    form = await req.form()

    file = form.get("file")
    if not isinstance(file, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="file is required")

    backend_class = str(form.get("backend_class") or form.get("backend") or getattr(S, "TRANSCRIPTION_BACKEND_CLASS", "local_mlx")).strip()
    if not backend_class:
        backend_class = "local_mlx"

    check_backend_ready(backend_class, route_kind="transcription")
    await check_capability(backend_class, "transcription")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "transcription")
    try:
        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="file is empty")

        form_fields: dict[str, str] = {}
        for key, value in form.multi_items():
            if key in {"file", "backend", "backend_class"}:
                continue
            if isinstance(value, StarletteUploadFile):
                continue
            if value is None:
                continue
            form_fields[str(key)] = str(value)

        if not form_fields.get("model"):
            configured_model = (getattr(S, "TRANSCRIPTION_MODEL", "") or "").strip()
            if configured_model:
                form_fields["model"] = configured_model

        kind, payload, content_type = await transcribe_openai_audio(
            backend_name=backend_class,
            file_name=file.filename or "audio",
            file_bytes=file_bytes,
            content_type=file.content_type or "application/octet-stream",
            form_fields=form_fields,
        )

        headers = {
            "X-Backend-Used": backend_class,
        }
        if kind == "json":
            return JSONResponse(payload if isinstance(payload, dict) else {"text": str(payload)}, headers=headers)
        return PlainTextResponse(str(payload), media_type=content_type, headers=headers)
    finally:
        admission.release(backend_class, "transcription")
