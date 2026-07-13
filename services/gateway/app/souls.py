from __future__ import annotations

import logging
import re
from pathlib import Path

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage


logger = logging.getLogger("uvicorn.error")
_SOUL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FORBIDDEN_CONTROL_TOKENS = (
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "<|assistant|>",
    "<<sys>>",
    "[inst]",
    "[/inst]",
)


def load_soul(name: str) -> str:
    soul_name = str(name or "").strip().lower()
    if not soul_name:
        return ""
    if not _SOUL_NAME_RE.fullmatch(soul_name):
        logger.error("Ignoring invalid Nexus soul name: %r", soul_name)
        return ""

    root = Path(S.NEXUS_SOUL_ROOT).expanduser().resolve()
    path = (root / soul_name / "SOUL.md").resolve()
    if root not in path.parents or not path.is_file():
        logger.error("Configured Nexus soul is missing: %s", path)
        return ""

    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Unable to read Nexus soul %s: %s", soul_name, exc)
        return ""
    if not content or "\x00" in content:
        logger.error("Ignoring empty or invalid Nexus soul: %s", soul_name)
        return ""

    lowered = content.lower()
    if any(token in lowered for token in _FORBIDDEN_CONTROL_TOKENS):
        logger.error("Ignoring Nexus soul containing model control tokens: %s", soul_name)
        return ""

    max_chars = max(256, int(S.NEXUS_SOUL_MAX_CHARS))
    if len(content) > max_chars:
        logger.warning("Truncating Nexus soul %s from %s to %s characters", soul_name, len(content), max_chars)
        content = content[:max_chars].rstrip()
    return content


def apply_alias_soul(req: ChatCompletionRequest, alias: ModelAlias | None) -> ChatCompletionRequest:
    soul_name = str(getattr(alias, "soul", "") or "")
    if not soul_name:
        return req
    content = load_soul(soul_name)
    if not content:
        return req
    if req.messages and req.messages[0].role == "system" and req.messages[0].content == content:
        return req
    return req.model_copy(update={"messages": [ChatMessage(role="system", content=content), *req.messages]})
