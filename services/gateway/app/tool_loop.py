from __future__ import annotations

import json
from typing import Any, Dict, Literal

from fastapi import HTTPException

from app.models import ChatCompletionRequest, ChatMessage
from app.tools_bus import run_tool_call
from app.upstreams import call_mlx_openai, call_ollama


async def tool_loop(
    initial_req: ChatCompletionRequest,
    backend: Literal["ollama", "mlx"],
    model_name: str,
    max_steps: int = 8,
    allowed_tools: set[str] | None = None,
) -> Dict[str, Any]:
    req = initial_req
    for _ in range(max_steps):
        if backend == "mlx":
            resp = await call_mlx_openai(req)
        else:
            resp = await call_ollama(req, model_name)

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {}) or {}
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            return resp

        new_messages = list(req.messages)
        new_messages.append(ChatMessage(**msg))

        for tc in tool_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name")
            arguments = fn.get("arguments", "")
            result = run_tool_call(name, arguments, allowed_tools=allowed_tools)
            new_messages.append(ChatMessage(role="tool", tool_call_id=tc.get("id"), content=json.dumps(result)))

        req = ChatCompletionRequest(
            model=req.model,
            messages=new_messages,
            tools=req.tools,
            tool_choice=req.tool_choice,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            stream=False,
        )

    raise HTTPException(status_code=500, detail="tool loop exceeded max_steps")
