from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import S


_SAFE_ID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def _now() -> int:
    return int(time.time())


def _ui_chat_dir() -> str:
    return (getattr(S, "UI_CHAT_DIR", "") or "/var/lib/gateway/data/ui_chats").strip() or "/var/lib/gateway/data/ui_chats"


def _ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_CHAT_TTL_SEC", 0) or 0)
    except Exception:
        return 0


def _max_bytes() -> int:
    try:
        return int(getattr(S, "UI_CHAT_MAX_BYTES", 0) or 0)
    except Exception:
        return 0


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _is_safe_id(conversation_id: str) -> bool:
    s = (conversation_id or "").strip()
    if not s or len(s) > 128:
        return False
    return all(c in _SAFE_ID_CHARS for c in s)


def _path_for(conversation_id: str) -> str:
    base = _ui_chat_dir()
    return str(Path(base).joinpath(f"{conversation_id}.json"))


def cleanup_expired() -> None:
    ttl = _ttl_sec()
    if ttl <= 0:
        return

    base = _ui_chat_dir()
    _ensure_dir(base)
    cutoff = time.time() - float(ttl)

    try:
        for name in os.listdir(base):
            if not name.endswith(".json"):
                continue
            full = os.path.join(base, name)
            try:
                st = os.stat(full)
                if st.st_mtime < cutoff:
                    os.remove(full)
            except FileNotFoundError:
                continue
            except Exception:
                continue
    except Exception:
        return


def new_conversation_id() -> str:
    # URL-safe, filesystem-safe.
    cid = secrets.token_urlsafe(18).replace("-", "_")
    return cid


@dataclass
class Conversation:
    id: str
    created: int
    updated: int
    summary: str
    messages: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "updated": self.updated,
            "summary": self.summary,
            "messages": self.messages,
        }


def load(conversation_id: str) -> Optional[Conversation]:
    if not _is_safe_id(conversation_id):
        return None

    cleanup_expired()
    path = _path_for(conversation_id)
    try:
        raw = Path(path).read_text(encoding="utf-8")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        cid = str(obj.get("id") or "").strip()
        if cid != conversation_id:
            return None
        created = int(obj.get("created") or _now())
        updated = int(obj.get("updated") or created)
        summary = str(obj.get("summary") or "")
        msgs = obj.get("messages")
        if not isinstance(msgs, list):
            msgs = []
        return Conversation(id=cid, created=created, updated=updated, summary=summary, messages=list(msgs))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def create() -> Conversation:
    cleanup_expired()
    base = _ui_chat_dir()
    _ensure_dir(base)

    cid = new_conversation_id()
    now = _now()
    convo = Conversation(id=cid, created=now, updated=now, summary="", messages=[])
    save(convo)
    return convo


def save(convo: Conversation) -> None:
    base = _ui_chat_dir()
    _ensure_dir(base)
    path = _path_for(convo.id)

    payload = convo.to_dict()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    max_b = _max_bytes()
    if max_b > 0 and len(data.encode("utf-8")) > max_b:
        # Fail closed; caller should summarize/prune before saving.
        raise ValueError("conversation too large")

    tmp = f"{path}.tmp"
    Path(tmp).write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def append_message(conversation_id: str, msg: Dict[str, Any]) -> Conversation:
    convo = load(conversation_id)
    if convo is None:
        raise FileNotFoundError("conversation not found")

    if not isinstance(msg, dict):
        raise ValueError("message must be an object")

    role = str(msg.get("role") or "").strip() or "user"
    content = msg.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)

    entry: Dict[str, Any] = {
        "role": role,
        "content": content or "",
        "ts": int(msg.get("ts") or _now()),
    }

    # Optional structured fields.
    for k in ["type", "url", "backend", "model", "reason", "mime", "sha256", "filename", "bytes", "attachments"]:
        if k in msg and msg.get(k) is not None:
            entry[k] = msg.get(k)

    convo.messages.append(entry)
    convo.updated = _now()
    save(convo)
    return convo
