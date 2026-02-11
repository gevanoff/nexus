from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import require_bearer
from app.agent_runtime_v1 import load_transcript, run_agent_v1
from app.models import AgentRunRequest


router = APIRouter()


@router.post("/v1/agent/run")
async def agent_run(req: Request):
    require_bearer(req)
    body = await req.json()
    ar = AgentRunRequest(**body)
    payload, backend, upstream_model = await run_agent_v1(req=req, run_req=ar)

    out = JSONResponse(payload)
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = upstream_model
    return out


@router.get("/v1/agent/replay/{run_id}")
async def agent_replay(req: Request, run_id: str):
    require_bearer(req)
    return load_transcript(run_id)
