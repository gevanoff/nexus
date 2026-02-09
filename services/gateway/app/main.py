"""
Nexus Gateway - Container-based AI orchestration gateway

This is a minimal gateway implementation that demonstrates the standardized API pattern.
For the full-featured gateway, see the gevanoff/gateway repository.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment
GATEWAY_BEARER_TOKEN = os.getenv("GATEWAY_BEARER_TOKEN", "change-me")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "ollama")

# Initialize FastAPI app
app = FastAPI(
    title="Nexus Gateway",
    version="1.0.0",
    description="Container-based AI orchestration gateway"
)


# ===== Authentication =====

def verify_bearer_token(authorization: Optional[str] = Header(None)) -> bool:
    """Verify bearer token authentication"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = authorization[7:]  # Remove "Bearer " prefix
    
    if token != GATEWAY_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    return True


# ===== Health and Metadata Endpoints =====

@app.get("/health")
async def health():
    """Liveness check - is the service running?"""
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    """Readiness check - can the service handle requests?"""
    checks = {}
    
    # Check Ollama backend
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            checks["ollama"] = "ok" if response.status_code == 200 else "error"
    except Exception as e:
        checks["ollama"] = f"error: {str(e)}"
    
    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks}
    )


@app.get("/v1/metadata")
async def metadata():
    """Service discovery - advertise capabilities and endpoints"""
    return {
        "schema_version": "v1",
        "service": {
            "name": "gateway",
            "version": "1.0.0",
            "description": "Nexus Gateway - AI orchestration API gateway"
        },
        "endpoints": [
            {
                "path": "/health",
                "method": "GET",
                "operation_id": "health.check",
                "summary": "Liveness check"
            },
            {
                "path": "/readyz",
                "method": "GET",
                "operation_id": "readiness.check",
                "summary": "Readiness check"
            },
            {
                "path": "/v1/metadata",
                "method": "GET",
                "operation_id": "metadata.get",
                "summary": "Service discovery"
            },
            {
                "path": "/v1/chat/completions",
                "method": "POST",
                "operation_id": "chat.completions.create",
                "summary": "Create chat completion"
            },
            {
                "path": "/v1/models",
                "method": "GET",
                "operation_id": "models.list",
                "summary": "List available models"
            }
        ],
        "capabilities": {
            "domains": ["chat", "completion", "embedding"],
            "modalities": ["text"],
            "streaming": True,
            "max_concurrency": 10
        },
        "backends": {
            "default": DEFAULT_BACKEND,
            "available": ["ollama"]
        }
    }


# ===== Models Endpoint =====

@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    """List available models from backend services"""
    verify_bearer_token(authorization)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get models from Ollama
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Backend unavailable")
            
            data = response.json()
            models = data.get("models", [])
            
            # Convert to OpenAI format
            model_list = [
                {
                    "id": model.get("name", "unknown"),
                    "object": "model",
                    "created": 0,
                    "owned_by": "ollama"
                }
                for model in models
            ]
            
            return {
                "object": "list",
                "data": model_list
            }
    
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(status_code=502, detail=f"Error listing models: {str(e)}")


# ===== Chat Completions Endpoint =====

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None)
):
    """Create a chat completion"""
    verify_bearer_token(authorization)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Convert to Ollama format
            ollama_request = {
                "model": request.model,
                "messages": [
                    {"role": msg.role, "content": msg.content}
                    for msg in request.messages
                ],
                "stream": request.stream,
                "options": {
                    "temperature": request.temperature
                }
            }
            
            if request.max_tokens:
                ollama_request["options"]["num_predict"] = request.max_tokens
            
            # Forward to Ollama
            if request.stream:
                # Streaming response
                async def stream_response():
                    async with client.stream(
                        "POST",
                        f"{OLLAMA_BASE_URL}/api/chat",
                        json=ollama_request
                    ) as response:
                        async for line in response.aiter_lines():
                            if line.strip():
                                yield f"data: {line}\n\n"
                        yield "data: [DONE]\n\n"
                
                return StreamingResponse(
                    stream_response(),
                    media_type="text/event-stream"
                )
            else:
                # Non-streaming response
                response = await client.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json=ollama_request
                )
                
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail="Backend error"
                    )
                
                data = response.json()
                
                # Convert to OpenAI format
                return {
                    "id": "chatcmpl-" + os.urandom(12).hex(),
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": data.get("message", {}).get("content", "")
                            },
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    }
                }
    
    except Exception as e:
        logger.error(f"Error in chat completion: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Error processing request: {str(e)}"
        )


# ===== Root endpoint =====

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Nexus Gateway",
        "version": "1.0.0",
        "endpoints": [
            "/health",
            "/readyz",
            "/v1/metadata",
            "/v1/models",
            "/v1/chat/completions"
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
