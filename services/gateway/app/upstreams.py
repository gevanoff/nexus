from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Literal

import httpx
from fastapi import HTTPException
from app.config import S, logger
from app.httpx_client import httpx_client as _httpx_client
from app.models import ChatCompletionRequest
from app.openai_utils import new_id, now_unix, sse, sse_done
from app.streaming import ollama_ndjson_to_openai_sse, passthrough_sse


async def call_mlx_openai(req: ChatCompletionRequest) -> Dict[str, Any]:
    def _normalize_messages_for_mlx(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        last_role: str | None = None
        for m in msgs:
            role = (m.get("role") or "").strip()
            # MLX expects strict user/assistant alternation; convert system->user
            if role == "system":
                role = "user"

            # Normalize content to string for safe merging
            content = m.get("content")
            if content is None:
                content_str = ""
            elif isinstance(content, str):
                content_str = content
            else:
                try:
                    content_str = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content_str = str(content)

            if last_role is not None and last_role == role and out:
                # Merge consecutive messages of same role to enforce alternation
                prev = out[-1]
                prev_content = prev.get("content") or ""
                if not isinstance(prev_content, str):
                    try:
                        prev_content = json.dumps(prev_content, ensure_ascii=False)
                    except Exception:
                        prev_content = str(prev_content)
                # Join with newline to preserve separation
                prev["content"] = prev_content + "\n" + content_str
            else:
                out.append({"role": role, "content": content_str})
                last_role = role
        return out

    payload = req.model_dump(exclude_none=True)
    # Normalize messages to satisfy MLX role alternation constraints
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = _normalize_messages_for_mlx(payload["messages"])

    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(f"{S.MLX_BASE_URL}/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "mlx", "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": "mlx", "error": str(e)})


async def call_ollama(req: ChatCompletionRequest, model_name: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
        "stream": False,
    }
    if req.tools:
        payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
    if req.temperature is not None:
        payload.setdefault("options", {})["temperature"] = req.temperature

    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(f"{S.OLLAMA_BASE_URL}/api/chat", json=payload)
            r.raise_for_status()
            out = r.json()
            if isinstance(out, dict) and isinstance(out.get("error"), str) and out.get("error"):
                raise HTTPException(status_code=502, detail={"upstream": "ollama", "error": out.get("error")})
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "ollama", "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": "ollama", "error": str(e)})

    msg = out.get("message", {})
    return {
        "id": new_id("chatcmpl"),
        "object": "chat.completion",
        "created": now_unix(),
        "choices": [{"index": 0, "message": msg, "finish_reason": out.get("done_reason", "stop")}],
        "model": model_name,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def embed_ollama(texts: List[str], model: str) -> List[List[float]]:
    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(
                f"{S.OLLAMA_BASE_URL}/api/embed",
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            j = r.json()
            embs = j.get("embeddings")
            if isinstance(embs, list) and embs and isinstance(embs[0], list):
                return embs
        except Exception:
            pass

        out: List[List[float]] = []
        for t in texts:
            r = await client.post(
                f"{S.OLLAMA_BASE_URL}/api/embeddings",
                json={"model": model, "prompt": t},
            )
            r.raise_for_status()
            j = r.json()
            e = j.get("embedding")
            if not isinstance(e, list):
                raise HTTPException(status_code=502, detail={"upstream": "ollama", "error": "No embedding in response"})
            out.append(e)
        return out


async def embed_mlx(texts: List[str], model: str) -> List[List[float]]:
    async with _httpx_client(timeout=600) as client:
        r = await client.post(
            f"{S.MLX_BASE_URL}/embeddings",
            json={"model": model, "input": texts if len(texts) > 1 else texts[0]},
        )
        r.raise_for_status()
        j = r.json()
        data = j.get("data", [])
        out: List[List[float]] = []
        for item in data:
            emb = (item or {}).get("embedding")
            if isinstance(emb, list):
                out.append(emb)
        if len(out) != len(texts):
            raise HTTPException(status_code=502, detail={"upstream": "mlx", "error": "Unexpected embeddings shape"})
        return out


async def embed_text_for_memory(text: str) -> list[float]:
    model = S.EMBEDDINGS_MODEL
    if S.EMBEDDINGS_BACKEND == "ollama":
        return (await embed_ollama([text], model))[0]
    return (await embed_mlx([text], model))[0]


async def stream_mlx_openai_chat(payload: Dict[str, Any]) -> AsyncIterator[bytes]:
    def _normalize_messages_for_mlx(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        last_role: str | None = None
        for m in msgs:
            role = (m.get("role") or "").strip()
            if role == "system":
                role = "user"
            content = m.get("content")
            if content is None:
                content_str = ""
            elif isinstance(content, str):
                content_str = content
            else:
                try:
                    content_str = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content_str = str(content)

            if last_role is not None and last_role == role and out:
                prev = out[-1]
                prev_content = prev.get("content") or ""
                if not isinstance(prev_content, str):
                    try:
                        prev_content = json.dumps(prev_content, ensure_ascii=False)
                    except Exception:
                        prev_content = str(prev_content)
                prev["content"] = prev_content + "\n" + content_str
            else:
                out.append({"role": role, "content": content_str})
                last_role = role
        return out

    # Normalize messages in-place if present
    if "messages" in payload and isinstance(payload["messages"], list):
        payload = dict(payload)
        payload["messages"] = _normalize_messages_for_mlx(payload["messages"])

    async with _httpx_client(timeout=None) as client:
        try:
            async with client.stream(
                "POST",
                f"{S.MLX_BASE_URL}/chat/completions",
                json=payload,
                headers={"accept": "text/event-stream"},
            ) as r:
                r.raise_for_status()
                async for chunk in passthrough_sse(r):
                    yield chunk
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "mlx", "status": e.response.status_code, "body": e.response.text[:5000]}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": "mlx", "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()


async def stream_ollama_chat_as_openai(req: ChatCompletionRequest, model_name: str) -> AsyncIterator[bytes]:
    chunk_id = new_id("chatcmpl")
    created = now_unix()
    model_id = f"ollama:{model_name}"

    finish_sent = False

    # Emit an initial chunk immediately so SSE clients (incl OpenAI SDK) always
    # see at least one event even if the upstream stream errors or yields no bytes.
    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    try:
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": True,
        }
        if req.tools:
            payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
        if req.temperature is not None:
            payload.setdefault("options", {})["temperature"] = req.temperature

        async with _httpx_client(timeout=None) as client:
            async with client.stream("POST", f"{S.OLLAMA_BASE_URL}/api/chat", json=payload) as r:
                r.raise_for_status()
                # For OpenAI-compatible responses, keep the backend prefix so clients can
                # correlate streamed chunks with /v1/models IDs.
                async for chunk in ollama_ndjson_to_openai_sse(
                    r,
                    model_name=model_id,
                    chunk_id=chunk_id,
                    created=created,
                    emit_role_chunk=False,
                ):
                    # Never forward upstream [DONE] directly; we emit exactly one [DONE]
                    # at the end so it cannot appear before a finish_reason chunk.
                    if chunk == sse_done():
                        break
                    # Best-effort: if we see a non-null finish_reason emitted by the translator,
                    # treat the stream as having a finish marker.
                    if b'"finish_reason":"' in chunk:
                        finish_sent = True
                    yield chunk

    except asyncio.CancelledError:
        raise
    except httpx.HTTPStatusError as e:
        detail = {"upstream": "ollama", "status": e.response.status_code, "body": e.response.text[:5000]}
        yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
    except httpx.RequestError as e:
        detail = {"upstream": "ollama", "error": str(e)}
        yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
    except Exception as e:
        logger.exception("Unexpected error in Ollama streaming")
        detail = {"upstream": "ollama", "error": str(e)}
        yield sse({"error": {"message": "Gateway streaming error", "type": "internal_error", "param": None, "code": None, "detail": detail}})
    finally:
        if not finish_sent:
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            finish_sent = True

        yield sse_done()


# httpx client factory is provided by app.httpx_client.httpx_client
