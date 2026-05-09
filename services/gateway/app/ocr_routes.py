from __future__ import annotations

from fastapi import APIRouter, Request

from app.auth import require_bearer
from app.ocr_backend import scan_ocr


router = APIRouter()


@router.post("/v1/ocr")
async def ocr(req: Request):
    require_bearer(req)
    body = await req.json()
    return await scan_ocr(body)


@router.post("/v1/scan")
async def scan(req: Request):
    require_bearer(req)
    body = await req.json()
    return await scan_ocr(body)
