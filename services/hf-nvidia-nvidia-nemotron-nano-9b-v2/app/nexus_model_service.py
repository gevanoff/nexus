from __future__ import annotations
import asyncio, json, os, time, uuid, logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("HF_MODEL_ID", "nvidia/NVIDIA-Nemotron-Nano-9B-v2")
MAX_NEW_TOKENS = int(os.environ.get("MAX_NEW_TOKENS", "2048"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.7"))
TOP_P = float(os.environ.get("TOP_P", "0.9"))
TOP_K = int(os.environ.get("TOP_K", "50"))
REPEAT_PENALTY = float(os.environ.get("REPEAT_PENALTY", "1.0"))
DEVICE = os.environ.get("DEVICE", "cuda")
HEALTHY = False


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "nvidia/NVIDIA-Nemotron-Nano-9B-v2"
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: Optional[int] = 2048
    top_p: float = 0.9
    top_k: int = 50
    repeat_penalty: float = 1.0
    stream: bool = False


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]


# --- Lazy model loading ---
_model = None
_tokenizer = None


def get_model():
    global _model, _tokenizer
    if _model is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        logger.info(f"Loading model {MODEL_ID} on {DEVICE}...")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)
        _model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            torch_dtype="auto",
            device_map=DEVICE,
            trust_remote_code=True,
        )
        _model.eval()
        global HEALTHY
        HEALTHY = True
        logger.info("Model loaded and healthy.")
    return _model, _tokenizer


def format_prompt(messages):
    """Format chat messages for Nemotron using chat template."""
    tok = get_model()[1]
    if hasattr(tok, 'apply_chat_template') and tok.chat_template:
        text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        # Fallback: simple role/content formatting
        parts = []
        for m in messages:
            role = m.role
            parts.append(f"<|{role}|>\n{m.content}")
        parts.append("<|assistant|>\n")
        text = "".join(parts)
    return text


app = FastAPI(title="NVIDIA Nemotron Nano 9B v2", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not HEALTHY:
        raise HTTPException(status_code=503, detail="model not loaded")
    return {"status": "ready"}