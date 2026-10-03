from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path

from app.config import S


_SAFE_AUDIO_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def audio_cache_dir() -> Path:
    raw = (getattr(S, "UI_AUDIO_DIR", "") or "/var/lib/gateway/data/ui_audio").strip()
    return Path(raw or "/var/lib/gateway/data/ui_audio").expanduser()


def audio_cache_ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_AUDIO_TTL_SEC", 900) or 900)
    except Exception:
        return 900


def audio_cache_max_bytes() -> int:
    try:
        return int(getattr(S, "UI_AUDIO_MAX_BYTES", 100_000_000) or 100_000_000)
    except Exception:
        return 100_000_000


def _audio_mime_to_ext(mime: str) -> str:
    value = (mime or "").lower().strip()
    return {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/weba": "weba",
        "audio/webm": "webm",
        "audio/mp4": "m4a",
        "audio/m4a": "m4a",
        "audio/x-m4a": "m4a",
        "audio/aac": "aac",
        "audio/flac": "flac",
    }.get(value, "bin")


def _cleanup_audio_cache(root: Path, *, ttl_sec: int) -> None:
    if ttl_sec <= 0:
        return
    cutoff = time.time() - float(ttl_sec)
    try:
        for path in root.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                continue
    except Exception:
        return


def save_audio_cache(*, audio_bytes: bytes, mime_hint: str) -> tuple[str, str, Path]:
    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise ValueError("audio_bytes must be bytes")
    payload = bytes(audio_bytes)
    max_bytes = audio_cache_max_bytes()
    if len(payload) > max_bytes:
        raise ValueError(f"audio too large to cache ({len(payload)} bytes > {max_bytes})")

    root = audio_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_audio_cache(root, ttl_sec=audio_cache_ttl_sec())

    sha256 = hashlib.sha256(payload).hexdigest()
    ext = _audio_mime_to_ext(mime_hint)
    name = f"a{secrets.token_urlsafe(18).replace('-', '_')}.{ext}"
    if not _SAFE_AUDIO_FILE_RE.fullmatch(name):
        raise ValueError("failed to generate safe filename")

    tmp = root / f".{name}.tmp"
    dst = root / name
    with tmp.open("wb") as handle:
        handle.write(payload)
    os.replace(tmp, dst)
    return name, sha256, dst


def resolve_audio_cache_path(name: str) -> Path | None:
    raw = str(name or "").strip()
    if not _SAFE_AUDIO_FILE_RE.fullmatch(raw):
        return None
    root = audio_cache_dir().resolve()
    candidate = (root / raw).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        return None
    return candidate
