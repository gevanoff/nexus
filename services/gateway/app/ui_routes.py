from __future__ import annotations

import base64
import io
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from app.httpx_client import httpx_client as _httpx_client
import subprocess
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse

from app.backends import check_capability, get_admission_controller, get_registry, _capability_availability
from app.config import S
from app.health_checker import check_backend_ready, get_health_checker
from app.model_aliases import get_aliases
from app.models import ChatCompletionRequest, ChatMessage
from app.openai_utils import now_unix, sse, sse_done
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_mlx_openai, call_ollama, stream_mlx_openai_chat, stream_ollama_chat_as_openai
from app.images_backend import generate_images
from app.tts_backend import generate_tts, _effective_tts_base_url
from app import ui_conversations
from app import user_store


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/favicon.ico", include_in_schema=False)
async def ui_favicon(req: Request):
    # Serve a root /favicon.ico so browsers that request it directly get our icon.
    try:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "static", "favicon.ico")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="image/x-icon", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="failed to serve favicon")


@router.get("/apple-touch-icon.png", include_in_schema=False)
async def ui_apple_touch(req: Request):
    try:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "static", "apple-touch-icon.png")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="failed to serve apple touch icon")


_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _peer_ip(req: Request) -> str:
    try:
        c = req.client
        return (c.host or "").strip() if c else ""
    except Exception:
        return ""


def _parse_forwarded_for(header_value: str) -> str:
    raw = (header_value or "").strip()
    if not raw:
        return ""
    for part in raw.split(","):
        token = part.strip().strip('"').strip()
        if not token:
            continue
        if token.startswith("[") and token.endswith("]"):
            token = token[1:-1].strip()
        if token.startswith("for="):
            token = token[4:].strip().strip('"').strip()
        if token.startswith("[") and "]" in token:
            token = token[1: token.index("]")]
        if token.count(":") == 1 and token.rsplit(":", 1)[1].isdigit() and "." in token:
            token = token.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(token)
            return token
        except Exception:
            continue
    return ""


def _parse_ip_allowlist(raw: str) -> list[Any]:
    items: list[Any] = []
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            # Accept bare IPs and CIDRs.
            if "/" in s:
                items.append(ipaddress.ip_network(s, strict=False))
            else:
                items.append(ipaddress.ip_address(s))
        except Exception:
            continue
    return items


def _ip_matches_allowlist(ip_s: str, allow: list[Any]) -> bool:
    try:
        ip = ipaddress.ip_address((ip_s or "").strip())
    except Exception:
        return False
    for item in allow:
        try:
            if isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                if ip == item:
                    return True
            else:
                if ip in item:
                    return True
        except Exception:
            continue
    return False


def _client_ip(req: Request) -> str:
    peer = _peer_ip(req)
    trusted_raw = (getattr(S, "UI_TRUST_PROXY_CIDRS", "") or "").strip()
    trusted = _parse_ip_allowlist(trusted_raw)
    if not trusted or not _ip_matches_allowlist(peer, trusted):
        return peer

    try:
        xff = (req.headers.get("x-forwarded-for") or "").strip()
    except Exception:
        xff = ""
    parsed_xff = _parse_forwarded_for(xff)
    if parsed_xff:
        return parsed_xff

    try:
        xri = (req.headers.get("x-real-ip") or "").strip()
    except Exception:
        xri = ""
    parsed_xri = _parse_forwarded_for(xri)
    if parsed_xri:
        return parsed_xri

    try:
        fwd = (req.headers.get("forwarded") or "").strip()
    except Exception:
        fwd = ""
    parsed_fwd = _parse_forwarded_for(fwd)
    if parsed_fwd:
        return parsed_fwd

    return peer


def _ui_deny_detail(req: Request, message: str) -> Any:
    if not bool(getattr(S, "UI_IP_ALLOWLIST_DEBUG", False)):
        return message
    try:
        headers = req.headers
    except Exception:
        headers = {}
    return {
        "error": "ui_access_denied",
        "message": message,
        "client_ip": _client_ip(req),
        "peer_ip": _peer_ip(req),
        "x_forwarded_for": (headers.get("x-forwarded-for") or "").strip(),
        "x_real_ip": (headers.get("x-real-ip") or "").strip(),
        "forwarded": (headers.get("forwarded") or "").strip(),
        "ui_ip_allowlist": (getattr(S, "UI_IP_ALLOWLIST", "") or "").strip(),
        "ui_trust_proxy_cidrs": (getattr(S, "UI_TRUST_PROXY_CIDRS", "") or "").strip(),
    }


def _require_ui_access(req: Request) -> None:
    raw = (getattr(S, "UI_IP_ALLOWLIST", "") or "").strip()
    if not raw:
        raise HTTPException(status_code=403, detail=_ui_deny_detail(req, "UI disabled (set UI_IP_ALLOWLIST to trusted IPs/CIDRs)"))

    ip_s = _client_ip(req)
    try:
        ip = ipaddress.ip_address(ip_s)
    except Exception:
        raise HTTPException(status_code=403, detail=_ui_deny_detail(req, "UI denied (unknown client IP)"))

    allow = _parse_ip_allowlist(raw)
    for item in allow:
        try:
            if isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                if ip == item:
                    return
            else:
                if ip in item:
                    return
        except Exception:
            continue

    raise HTTPException(status_code=403, detail=_ui_deny_detail(req, "UI denied (client IP not allowlisted)"))


def _session_cookie_name() -> str:
    return (getattr(S, "USER_SESSION_COOKIE", "") or "gateway_session").strip() or "gateway_session"


def _session_token_from_req(req: Request) -> str:
    try:
        token = (req.headers.get("authorization") or "").strip()
        if token.lower().startswith("bearer "):
            return token.split(" ", 1)[1].strip()
    except Exception:
        token = ""

    try:
        token = (req.headers.get("x-session-token") or "").strip()
        if token:
            return token
    except Exception:
        token = ""

    try:
        cookie_name = _session_cookie_name()
        return (req.cookies or {}).get(cookie_name) or ""
    except Exception:
        return ""


def _require_user(req: Request) -> Optional[user_store.User]:
    if not getattr(S, "USER_AUTH_ENABLED", True):
        return None
    token = _session_token_from_req(req)
    user = user_store.get_user_by_session(S.USER_DB_PATH, token=token)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        req.state.user = user
    except Exception:
        pass
    return user


def _require_admin(req: Request) -> user_store.User:
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if not getattr(user, "admin", False):
        raise HTTPException(status_code=403, detail="admin required")
    return user


def _coerce_tts_body(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        alt = body.get("input")
        if not isinstance(alt, str) or not alt.strip():
            raise HTTPException(status_code=400, detail="text is required")
    return body


def _resolve_tts_backend_class(req: Request, body: Optional[Dict[str, Any]] = None, *, explicit: Optional[str] = None) -> str:
    backend_class = (explicit or "").strip()
    if not backend_class and isinstance(body, dict):
        try:
            backend_class = str(body.get("backend_class") or body.get("backend") or "").strip()
        except Exception:
            backend_class = ""

    if not backend_class:
        try:
            user = _require_user(req)
        except HTTPException:
            user = None
        if user is not None:
            try:
                settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
                tts = settings.get("tts") if isinstance(settings, dict) else None
                if isinstance(tts, dict):
                    backend_class = str(tts.get("backend_class") or tts.get("backend") or "").strip()
                if not backend_class:
                    backend_class = str(settings.get("tts_backend") or settings.get("ttsBackend") or "").strip()
            except Exception:
                backend_class = ""

    if not backend_class:
        backend_class = (getattr(S, "TTS_BACKEND_CLASS", "") or "").strip() or "pocket_tts"

    reg = get_registry()
    cfg = reg.get_backend(backend_class)
    if not cfg or not cfg.supports("tts"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "tts_backend_unavailable",
                "backend_class": backend_class,
                **_capability_availability("tts"),
            },
        )
    return cfg.backend_class


def _effective_tts_clone_path(backend_class: str) -> str:
    b = (backend_class or "").lower()
    if "lux" in b:
        path = (getattr(S, "LUXTTS_CLONE_PATH", "") or "").strip()
    elif "qwen" in b:
        path = (getattr(S, "QWEN3_TTS_CLONE_PATH", "") or "").strip()
    else:
        path = (getattr(S, "TTS_CLONE_PATH", "") or "").strip()
    if not path:
        path = "/v1/audio/clone"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _tts_gateway_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return headers
    backend = meta.get("backend")
    backend_class = meta.get("backend_class")
    latency = meta.get("upstream_latency_ms")
    if backend:
        headers["x-gateway-backend"] = str(backend)
    if backend_class:
        headers["x-gateway-backend-class"] = str(backend_class)
    if latency is not None:
        headers["x-gateway-upstream-latency-ms"] = str(latency)
    return headers


def _ui_image_dir() -> str:
    return (getattr(S, "UI_IMAGE_DIR", "") or "/var/lib/gateway/data/ui_images").strip() or "/var/lib/gateway/data/ui_images"


def _ui_image_ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_IMAGE_TTL_SEC", 900) or 900)
    except Exception:
        return 900


def _ui_image_max_bytes() -> int:
    try:
        return int(getattr(S, "UI_IMAGE_MAX_BYTES", 50_000_000) or 50_000_000)
    except Exception:
        return 50_000_000


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _cleanup_ui_images(path: str, *, ttl_sec: int) -> None:
    # Best-effort cleanup; never fail the request for cleanup errors.
    if ttl_sec <= 0:
        return
    now = time.time()
    cutoff = now - float(ttl_sec)
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
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


def _mime_to_ext(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m == "image/png":
        return "png"
    if m == "image/jpeg":
        return "jpg"
    if m == "image/webp":
        return "webp"
    if m == "image/svg+xml":
        return "svg"
    return "bin"


def _cleanup_ui_files(path: str, *, ttl_sec: int) -> None:
    if ttl_sec <= 0:
        return
    now = time.time()
    cutoff = now - float(ttl_sec)
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
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


def _sniff_mime(raw: bytes) -> str | None:
    # Best-effort sniff based on magic bytes.
    if not raw:
        return None

    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    head = raw[:256].lstrip()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "image/svg+xml"
    return None


def _ui_audio_dir() -> str:
    return (getattr(S, "UI_AUDIO_DIR", "") or "/var/lib/gateway/data/ui_audio").strip() or "/var/lib/gateway/data/ui_audio"


def _ui_audio_ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_AUDIO_TTL_SEC", 900) or 900)
    except Exception:
        return 900


def _ui_file_dir() -> str:
    return (getattr(S, "UI_FILE_DIR", "") or "/var/lib/gateway/data/ui_files").strip() or "/var/lib/gateway/data/ui_files"


def _ui_file_ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_FILE_TTL_SEC", 0) or 0)
    except Exception:
        return 0


def _ui_file_max_bytes() -> int:
    try:
        return int(getattr(S, "UI_FILE_MAX_BYTES", 0) or 0)
    except Exception:
        return 0


def _ui_audio_max_bytes() -> int:
    try:
        return int(getattr(S, "UI_AUDIO_MAX_BYTES", 100_000_000) or 100_000_000)
    except Exception:
        return 100_000_000


def _cleanup_ui_audio(path: str, *, ttl_sec: int) -> None:
    if ttl_sec <= 0:
        return
    now = time.time()
    cutoff = now - float(ttl_sec)
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
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


def _audio_mime_to_ext(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m in ("audio/wav", "audio/x-wav"):
        return "wav"
    if m in ("audio/mpeg", "audio/mp3"):
        return "mp3"
    if m == "audio/ogg":
        return "ogg"
    if m == "audio/webm":
        return "webm"
    return "bin"


def _save_ui_audio(*, audio_bytes: bytes, mime_hint: str) -> tuple[str, str]:
    audio_dir = _ui_audio_dir()
    ttl_sec = _ui_audio_ttl_sec()
    max_bytes = _ui_audio_max_bytes()
    _ensure_dir(audio_dir)
    _cleanup_ui_audio(audio_dir, ttl_sec=ttl_sec)

    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise ValueError("audio_bytes must be bytes")
    if len(audio_bytes) > max_bytes:
        raise ValueError(f"audio too large to cache ({len(audio_bytes)} bytes > {max_bytes})")

    sha256 = hashlib.sha256(bytes(audio_bytes)).hexdigest()
    mime = (mime_hint or "audio/wav").strip()
    ext = _audio_mime_to_ext(mime)
    name = f"{secrets.token_urlsafe(18)}.{ext}"
    name = name.replace("-", "_")
    if not _SAFE_FILE_RE.match(name):
        raise ValueError("failed to generate safe filename")

    tmp = os.path.join(audio_dir, f".{name}.tmp")
    dst = os.path.join(audio_dir, name)
    with open(tmp, "wb") as f:
        f.write(audio_bytes)
    os.replace(tmp, dst)
    return f"/ui/audio/{name}", sha256


def _voice_library_dir() -> str:
    return (getattr(S, "VOICE_LIBRARY_DIR", "") or "/var/lib/gateway/data/voice_library").strip() or "/var/lib/gateway/data/voice_library"


def _voice_library_max_bytes() -> int:
    try:
        return int(getattr(S, "VOICE_LIBRARY_MAX_BYTES", 50_000_000) or 50_000_000)
    except Exception:
        return 50_000_000


def _safe_voice_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    clean = clean.strip("._-")
    return clean[:48] if clean else "voice"


def _save_voice_sample(*, name: str, audio_bytes: bytes, mime_hint: str) -> Dict[str, Any]:
    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise ValueError("audio_bytes must be bytes")
    if len(audio_bytes) > _voice_library_max_bytes():
        raise ValueError("audio exceeds voice library limit")

    lib = _voice_library_dir()
    _ensure_dir(lib)
    mime = (mime_hint or "audio/wav").strip()
    ext = _audio_mime_to_ext(mime)
    safe_name = _safe_voice_name(name)
    voice_id = f"{safe_name}_{secrets.token_urlsafe(8)}".replace("-", "_")
    fname = f"{voice_id}.{ext}"
    path = os.path.join(lib, fname)
    with open(path, "wb") as f:
        f.write(bytes(audio_bytes))
    return {"id": voice_id, "name": safe_name, "filename": fname, "mime": mime, "bytes": len(audio_bytes)}


def _list_voice_samples() -> list[Dict[str, Any]]:
    lib = _voice_library_dir()
    if not os.path.isdir(lib):
        return []
    out: list[Dict[str, Any]] = []
    for name in os.listdir(lib):
        if not name or name.startswith("."):
            continue
        full = os.path.join(lib, name)
        if not os.path.isfile(full):
            continue
        voice_id, _sep, _ext = name.partition(".")
        out.append({"id": voice_id, "filename": name})
    out.sort(key=lambda x: x.get("id") or "")
    return out


def _load_voice_sample(voice_id: str) -> tuple[bytes, str] | None:
    lib = _voice_library_dir()
    if not os.path.isdir(lib):
        return None
    safe = _safe_voice_name(voice_id)
    # accept id prefixes to locate any extension
    for name in os.listdir(lib):
        if not name or name.startswith("."):
            continue
        if name.startswith(safe):
            full = os.path.join(lib, name)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as f:
                data = f.read()
            mime = "audio/wav"
            ext = name.rsplit(".", 1)[-1].lower()
            if ext == "mp3":
                mime = "audio/mpeg"
            elif ext == "ogg":
                mime = "audio/ogg"
            elif ext == "webm":
                mime = "audio/webm"
            return data, mime
    return None


def _delete_voice_sample(voice_id: str) -> bool:
    lib = _voice_library_dir()
    if not os.path.isdir(lib):
        return False
    safe = _safe_voice_name(voice_id)
    removed = False
    for name in os.listdir(lib):
        if name.startswith(safe):
            full = os.path.join(lib, name)
            try:
                os.remove(full)
                removed = True
            except Exception:
                continue
    return removed


def _gateway_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return headers
    backend = meta.get("backend")
    backend_class = meta.get("backend_class")
    latency = meta.get("upstream_latency_ms")
    if backend:
        headers["x-gateway-backend"] = str(backend)
    if backend_class:
        headers["x-gateway-backend-class"] = str(backend_class)
    if latency is not None:
        headers["x-gateway-upstream-latency-ms"] = str(latency)
    return headers


def _decode_image_b64(b64_or_data_url: str) -> tuple[bytes, str | None]:
    s = (b64_or_data_url or "").strip()
    if not s:
        raise ValueError("empty image data")

    if s.startswith("data:"):
        # data:<mime>;base64,<payload>
        try:
            header, payload = s.split(",", 1)
        except ValueError:
            raise ValueError("invalid data URL")

        mime = None
        try:
            header2 = header[5:]
            parts = header2.split(";")
            if parts:
                mime = parts[0].strip() or None
        except Exception:
            mime = None

        raw = base64.b64decode(payload.encode("ascii"), validate=False)
        return raw, (mime or _sniff_mime(raw))

    raw2 = base64.b64decode(s.encode("ascii"), validate=False)
    return raw2, _sniff_mime(raw2)


def _save_ui_image(*, b64: str, mime_hint: str) -> tuple[str, str, str]:
    img_dir = _ui_image_dir()
    ttl_sec = _ui_image_ttl_sec()
    max_bytes = _ui_image_max_bytes()
    _ensure_dir(img_dir)
    _cleanup_ui_images(img_dir, ttl_sec=ttl_sec)

    raw, mime_from_data = _decode_image_b64(b64)
    if len(raw) > max_bytes:
        raise ValueError(f"image too large to cache ({len(raw)} bytes > {max_bytes})")

    sha256 = hashlib.sha256(raw).hexdigest()

    mime = (mime_from_data or mime_hint or "application/octet-stream").strip()
    ext = _mime_to_ext(mime)
    name = f"{secrets.token_urlsafe(18)}.{ext}"
    # Make the filename deterministic-safe.
    name = name.replace("-", "_")
    if not _SAFE_FILE_RE.match(name):
        # Extremely unlikely, but fail closed.
        raise ValueError("failed to generate safe filename")

    tmp = os.path.join(img_dir, f".{name}.tmp")
    dst = os.path.join(img_dir, name)
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, dst)

    return f"/ui/images/{name}", mime, sha256


def _safe_ext_from_filename(filename: str, mime: str) -> str:
    base_ext = ""
    if filename:
        _, ext = os.path.splitext(filename)
        if ext:
            base_ext = ext.lstrip(".").lower()
    if base_ext and re.fullmatch(r"[a-z0-9]{1,12}", base_ext):
        return base_ext
    guessed = mimetypes.guess_extension(mime or "") or ""
    guessed = guessed.lstrip(".").lower()
    if guessed and re.fullmatch(r"[a-z0-9]{1,12}", guessed):
        return guessed
    return "bin"


async def _save_ui_file(*, upload: UploadFile) -> Dict[str, Any]:
    file_dir = _ui_file_dir()
    ttl_sec = _ui_file_ttl_sec()
    max_bytes = _ui_file_max_bytes()
    _ensure_dir(file_dir)
    _cleanup_ui_files(file_dir, ttl_sec=ttl_sec)

    raw = await upload.read()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raw = b""
    if max_bytes > 0 and len(raw) > max_bytes:
        raise ValueError(f"file too large to cache ({len(raw)} bytes > {max_bytes})")

    sha256 = hashlib.sha256(raw).hexdigest()
    mime = (upload.content_type or "application/octet-stream").strip() or "application/octet-stream"
    ext = _safe_ext_from_filename(upload.filename or "", mime)
    name = f"{secrets.token_urlsafe(18)}.{ext}"
    name = name.replace("-", "_")
    if not _SAFE_FILE_RE.match(name):
        raise ValueError("failed to generate safe filename")

    tmp = os.path.join(file_dir, f".{name}.tmp")
    dst = os.path.join(file_dir, name)
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, dst)

    return {
        "filename": upload.filename or name,
        "url": f"/ui/files/{name}",
        "mime": mime,
        "bytes": len(raw),
        "sha256": sha256,
    }


@router.get("/ui", include_in_schema=False)
async def ui(req: Request) -> HTMLResponse:
    """Main UI entrypoint.

    We keep the legacy UI available at /ui1.
    """

    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat2.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/", include_in_schema=False)
async def ui_slash(req: Request) -> HTMLResponse:
    return await ui(req)



@router.get("/ui/login", include_in_schema=False)
async def ui_login(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("login.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/login/", include_in_schema=False)
async def ui_login_slash(req: Request) -> HTMLResponse:
    return await ui_login(req)



@router.get("/ui/image", include_in_schema=False)
async def ui_image_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("image.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/music", include_in_schema=False)
async def ui_music_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("music.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/video", include_in_schema=False)
async def ui_video_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("video.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/ocr", include_in_schema=False)
async def ui_ocr_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("ocr.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/scan", include_in_schema=False)
async def ui_scan_frontend(req: Request) -> HTMLResponse:
    return await ui_ocr_frontend(req)


@router.get("/ui/tts", include_in_schema=False)
async def ui_tts_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("tts.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/voice-clone", include_in_schema=False)
async def ui_voice_clone_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("voice_clone.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/personaplex", include_in_schema=False)
async def ui_personaplex_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("personaplex.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/admin/users", include_in_schema=False)
async def ui_admin_users(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    # Only render the page; the page will call the admin APIs which enforce admin privileges.
    html_path = Path(__file__).with_name("static").joinpath("admin_users.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.post("/ui/api/music", include_in_schema=False)
async def ui_api_music(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    # Best-effort: forward to music backend and return its normalized response.
    from app.music_backend import generate_music

    try:
        out = await generate_music(backend_class=getattr(S, "MUSIC_BACKEND_CLASS", "heartmula_music"), body=body)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"music backend failed: {e}")

    return out


@router.post("/ui/api/video", include_in_schema=False)
async def ui_api_video(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    request_id = getattr(req.state, "request_id", "")
    base = (getattr(S, "SKYREELS_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=404, detail="SkyReels base URL not configured")

    path = (getattr(S, "SKYREELS_GENERATE_PATH", "") or "/v1/videos/generations").strip()
    if not path.startswith("/"):
        path = "/" + path

    timeout = getattr(S, "SKYREELS_TIMEOUT_SEC", 3600.0) or 3600.0
    try:
        timeout = float(timeout)
    except Exception:
        timeout = 3600.0

    payload = _normalize_skyreels_payload(body)
    logger.info(
        "Video UI request forwarding request_id=%s base=%s path=%s payload_keys=%s",
        request_id,
        base,
        path,
        sorted(payload.keys()),
    )

    headers = {}
    if request_id:
        headers["X-Request-Id"] = request_id

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(f"{base}{path}", json=payload, headers=headers)
        except httpx.RequestError as exc:
            logger.warning(
                "SkyReels request error request_id=%s base=%s path=%s error=%s",
                request_id,
                base,
                path,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "skyreels_request_failed",
                    "message": str(exc),
                    "request_id": request_id,
                },
            )

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            logger.warning(
                "SkyReels upstream error request_id=%s status=%s body=%s",
                request_id,
                resp.status_code,
                data,
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail={
                    "error": "skyreels_upstream_error",
                    "status_code": resp.status_code,
                    "body": data,
                    "request_id": request_id,
                },
            )
        return data


def _normalize_skyreels_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    ui_keys = {"prompt", "duration", "resolution"}
    payload: Dict[str, Any]
    if set(body.keys()).issubset(ui_keys):
        payload = dict(body)
        prompt = str(body.get("prompt", "") or "").strip()
        if prompt:
            payload["prompt"] = prompt
        duration = body.get("duration")
        if duration is not None:
            try:
                payload["duration_seconds"] = max(1, int(duration))
            except Exception:
                pass
        width_height = _parse_resolution(body.get("resolution"))
        if width_height:
            payload["width"], payload["height"] = width_height
        return payload

    payload = dict(body)
    if "duration" in payload and "duration_seconds" not in payload:
        try:
            payload["duration_seconds"] = max(1, int(payload.get("duration", 0)))
        except Exception:
            pass
    if "resolution" in payload and ("width" not in payload or "height" not in payload):
        width_height = _parse_resolution(payload.get("resolution"))
        if width_height:
            payload.setdefault("width", width_height[0])
            payload.setdefault("height", width_height[1])
    return payload


def _parse_resolution(resolution: Any) -> Optional[Tuple[int, int]]:
    if not resolution:
        return None
    text = str(resolution).strip().lower()
    if "x" in text:
        parts = text.split("x", 1)
        try:
            return int(parts[0]), int(parts[1])
        except Exception:
            return None
    presets = {
        "480p": (854, 480),
        "720p": (1280, 720),
        "1080p": (1920, 1080),
    }
    return presets.get(text)


@router.post("/ui/api/personaplex/chat", include_in_schema=False)
async def ui_api_personaplex_chat(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    base = (getattr(S, "PERSONAPLEX_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        ui_url = (getattr(S, "PERSONAPLEX_UI_URL", "") or "").strip() or "https://localhost:8998"
        raise HTTPException(status_code=501, detail={"error": "personaplex_rest_unavailable", "ui_url": ui_url})

    timeout = getattr(S, "PERSONAPLEX_TIMEOUT_SEC", 120.0) or 120.0
    try:
        timeout = float(timeout)
    except Exception:
        timeout = 120.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base}/v1/chat/completions", json=body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=data)
        return data


@router.get("/ui/api/personaplex/info", include_in_schema=False)
async def ui_api_personaplex_info(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    ui_url = (getattr(S, "PERSONAPLEX_UI_URL", "") or "").strip() or "https://localhost:8998"
    base = (getattr(S, "PERSONAPLEX_BASE_URL", "") or "").strip().rstrip("/")
    return {"ui_url": ui_url, "rest_enabled": bool(base)}


@router.get("/ui/api/tts/backends", include_in_schema=False)
async def ui_api_tts_backends(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    return _capability_availability("tts")


@router.post("/ui/api/tts", include_in_schema=False)
async def ui_api_tts(req: Request):
    _require_ui_access(req)
    user = _require_user(req)
    body = _coerce_tts_body(await req.json())
    # If authenticated and no explicit voice provided, prefer the user's saved voice.
    try:
        if user is not None and not body.get("voice"):
            settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
            voice = None
            try:
                voice = (settings.get("tts") or {}).get("voice") if isinstance(settings, dict) else None
            except Exception:
                voice = None
            if not voice:
                voice = settings.get("tts_voice") or settings.get("ttsVoice")
            if isinstance(voice, str) and voice:
                body["voice"] = voice
    except Exception:
        # best-effort only; fall back to request-provided or backend default
        pass
    backend_class = _resolve_tts_backend_class(req, body)

    check_backend_ready(backend_class, route_kind="tts")
    await check_capability(backend_class, "tts")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "tts")
    try:
        result = await generate_tts(backend_class=backend_class, body=body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tts backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "tts")

    headers = _tts_gateway_headers(result.gateway)
    if result.kind == "json":
        payload = result.payload
        if isinstance(payload, dict):
            payload.setdefault("_gateway", {}).update(result.gateway)
        return JSONResponse(payload or {}, headers=headers)

    if result.audio is None:
        raise HTTPException(status_code=502, detail="tts backend returned no audio")
    # `result.audio` is raw bytes. Iterating a `bytes` yields ints which
    # Starlette's `stream_response` later tries to `.encode()` on strings and
    # will fail for ints. Wrap the bytes in an iterator that yields a single
    # bytes chunk so StreamingResponse sees a bytes-like chunk.
    # Also expose a temporary UI URL for chat consumers by caching the bytes
    try:
        url, _ = _save_ui_audio(audio_bytes=result.audio, mime_hint=result.content_type)
        # If successful, include a short helper link in headers for clients.
        headers.setdefault("X-Gateway-TTS-URL", url)
    except Exception:
        pass
    return StreamingResponse(iter([result.audio]), media_type=result.content_type, headers=headers)


@router.get("/ui/api/tts/voices", include_in_schema=False)
async def ui_api_tts_voices(req: Request):
    """Proxy the TTS backend's voices list for the UI.

    This is best-effort: different backends may expose different shapes. We
    try common voice-listing paths and return the first successful JSON.
    """
    _require_ui_access(req)
    _require_user(req)

    backend_class = _resolve_tts_backend_class(req, None, explicit=str(req.query_params.get("backend_class") or "").strip())
    base = _effective_tts_base_url(backend_class=backend_class)
    if not base:
        raise HTTPException(status_code=404, detail="tts backend not configured")

    # Special-case local pocket_tts which does not expose a voice-listing endpoint.
    # If pocket_tts is importable in-process and defines PREDEFINED_VOICES, return that.
    if backend_class and "pocket" in backend_class:
        try:
            from pocket_tts.utils.utils import PREDEFINED_VOICES  # type: ignore
            if PREDEFINED_VOICES:
                return JSONResponse(list(PREDEFINED_VOICES.keys()) if hasattr(PREDEFINED_VOICES, 'keys') else list(PREDEFINED_VOICES))
        except Exception:
            # If import fails in this Python environment, try invoking a known pocket-tts python
            # executable (common path used on deployments) to extract PREDEFINED_VOICES.
            candidates = []
            # allow explicit override via settings or environment
            try:
                p = (getattr(S, "POCKET_TTS_PYTHON", None) or os.environ.get("POCKET_TTS_PYTHON") or "").strip()
                if p:
                    candidates.append(p)
            except Exception:
                pass
            # common deployment path
            candidates.extend(["/var/lib/pocket-tts/env/bin/python", "/var/lib/pocket-tts/venv/bin/python"])

            for py in candidates:
                try:
                    args = [py, "-c", "import json; from pocket_tts.utils.utils import PREDEFINED_VOICES; print(json.dumps(list(PREDEFINED_VOICES.keys()) if hasattr(PREDEFINED_VOICES, 'keys') else list(PREDEFINED_VOICES)))"]
                    proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
                    if proc.returncode == 0 and proc.stdout:
                        try:
                            payload = json.loads(proc.stdout.strip())
                            if isinstance(payload, list):
                                return JSONResponse(payload)
                        except Exception:
                            # ignore parse errors and continue
                            pass
                except Exception:
                    continue

    async with _httpx_client(timeout=10) as client:
        last_err = None
        for p in ("/v1/voices", "/voices"):
            try:
                r = await client.get(f"{base}{p}")
                if 200 <= r.status_code < 300:
                    try:
                        return JSONResponse(r.json())
                    except Exception:
                        return JSONResponse({"data": r.text})
            except Exception as e:
                last_err = e

    raise HTTPException(status_code=502, detail=f"tts backend voices query failed: {last_err}")


@router.post("/ui/api/tts/clone", include_in_schema=False)
async def ui_api_tts_clone(
    req: Request,
    prompt_audio: UploadFile | None = File(None),
    text: str = Form(...),
    backend_class: str = Form(""),
    voice_name: str = Form(""),
    voice_id: str = Form(""),
    language: Optional[str] = Form(None),
    ref_text: Optional[str] = Form(None),
    ref_audio: Optional[str] = Form(None),
    voice_clone_prompt: Optional[str] = Form(None),
    x_vector_only_mode: Optional[str] = Form(None),
    max_new_tokens: Optional[str] = Form(None),
    top_p: Optional[str] = Form(None),
    rms: Optional[str] = Form(None),
    duration: Optional[str] = Form(None),
    num_steps: Optional[str] = Form(None),
    t_shift: Optional[str] = Form(None),
    speed: Optional[str] = Form(None),
    return_smooth: Optional[str] = Form(None),
):
    _require_ui_access(req)
    _require_user(req)

    if not text or not str(text).strip():
        raise HTTPException(status_code=400, detail="text is required")

    backend = _resolve_tts_backend_class(req, None, explicit=str(backend_class or "").strip())
    base = _effective_tts_base_url(backend_class=backend)
    if not base:
        raise HTTPException(status_code=404, detail="tts backend not configured")

    path = _effective_tts_clone_path(backend)

    data: Dict[str, Any] = {"text": str(text)}
    if language:
        data["language"] = str(language)
    if ref_text:
        data["ref_text"] = str(ref_text)
    if ref_audio:
        data["ref_audio"] = str(ref_audio)
    if voice_clone_prompt:
        data["voice_clone_prompt"] = str(voice_clone_prompt)
    if x_vector_only_mode:
        data["x_vector_only_mode"] = str(x_vector_only_mode)
    if max_new_tokens:
        data["max_new_tokens"] = str(max_new_tokens)
    if top_p:
        data["top_p"] = str(top_p)
    if rms:
        data["rms"] = str(rms)
    if duration:
        data["duration"] = str(duration)
    if num_steps:
        data["num_steps"] = str(num_steps)
    if t_shift:
        data["t_shift"] = str(t_shift)
    if speed:
        data["speed"] = str(speed)
    if return_smooth:
        data["return_smooth"] = str(return_smooth)

    file_bytes = b""
    file_mime = ""
    if prompt_audio is not None:
        file_bytes = await prompt_audio.read()
        file_mime = prompt_audio.content_type or "application/octet-stream"

    if not file_bytes and voice_id:
        loaded = _load_voice_sample(voice_id)
        if loaded is not None:
            file_bytes, file_mime = loaded

    if not file_bytes and not ref_audio and not voice_clone_prompt:
        raise HTTPException(status_code=400, detail="prompt_audio, voice_id, ref_audio, or voice_clone_prompt is required")

    files = None
    if file_bytes:
        files = {
            "prompt_audio": (
                (prompt_audio.filename if prompt_audio is not None else "prompt_audio"),
                file_bytes,
                file_mime or "application/octet-stream",
            )
        }

    timeout = getattr(S, "TTS_TIMEOUT_SEC", 120.0) or 120.0
    try:
        timeout = float(timeout)
    except Exception:
        timeout = 120.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base}{path}", data=data, files=files)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    if "application/json" in (content_type or ""):
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=payload)
        return JSONResponse(payload)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    # Cache and return audio response
    try:
        url, _ = _save_ui_audio(audio_bytes=resp.content, mime_hint=content_type)
        headers = {"X-Gateway-TTS-URL": url}
        if voice_name:
            try:
                saved = _save_voice_sample(name=voice_name, audio_bytes=file_bytes, mime_hint=file_mime or "audio/wav")
                headers["X-Gateway-Voice-Id"] = saved.get("id") or ""
            except Exception:
                pass
        return StreamingResponse(iter([resp.content]), media_type=content_type, headers=headers)
    except Exception:
        return StreamingResponse(iter([resp.content]), media_type=content_type)


@router.get("/ui/api/tts/voice-library", include_in_schema=False)
async def ui_api_voice_library_list(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    return {"voices": _list_voice_samples()}


@router.post("/ui/api/tts/voice-library", include_in_schema=False)
async def ui_api_voice_library_save(
    req: Request,
    voice_name: str = Form(""),
    prompt_audio: UploadFile = File(...),
):
    _require_ui_access(req)
    _require_user(req)
    if not voice_name:
        raise HTTPException(status_code=400, detail="voice_name is required")
    file_bytes = await prompt_audio.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="prompt_audio is required")
    saved = _save_voice_sample(name=voice_name, audio_bytes=file_bytes, mime_hint=prompt_audio.content_type or "audio/wav")
    return {"voice": saved}


@router.delete("/ui/api/tts/voice-library/{voice_id}", include_in_schema=False)
async def ui_api_voice_library_delete(req: Request, voice_id: str):
    _require_ui_access(req)
    _require_user(req)
    ok = _delete_voice_sample(voice_id)
    return {"ok": ok, "voice_id": voice_id}


@router.get("/ui/audio/{name}", include_in_schema=False)
async def ui_get_audio(req: Request, name: str):
    _require_ui_access(req)
    # Serve cached UI audio files written by _save_ui_audio.
    if not _SAFE_FILE_RE.match(name):
        raise HTTPException(status_code=404, detail="audio not found")
    audio_dir = _ui_audio_dir()
    path = os.path.join(audio_dir, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="audio not found")
    # Let FileResponse infer content-type from extension; fallback to octet-stream
    return FileResponse(path)


@router.post("/ui/api/auth/login", include_in_schema=False)
async def ui_auth_login(req: Request):
    _require_ui_access(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    user = user_store.authenticate(S.USER_DB_PATH, username=username, password=password)
    if user is None:
        raise HTTPException(status_code=403, detail="invalid credentials")
    ttl = int(getattr(S, "USER_SESSION_TTL_SEC", 0) or 0)
    if ttl <= 0:
        ttl = 60 * 60 * 12
    session = user_store.create_session(S.USER_DB_PATH, user_id=user.id, ttl_sec=ttl)
    resp = JSONResponse({"ok": True, "user": {"id": user.id, "username": user.username}})
    resp.set_cookie(
        _session_cookie_name(),
        session.token,
        max_age=ttl,
        httponly=True,
        secure=False,
        samesite="lax",
    )
    return resp


@router.post("/ui/api/auth/logout", include_in_schema=False)
async def ui_auth_logout(req: Request):
    _require_ui_access(req)
    token = _session_token_from_req(req)
    if token:
        user_store.delete_session(S.USER_DB_PATH, token=token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_session_cookie_name())
    return resp


@router.get("/ui/api/users", include_in_schema=False)
async def ui_api_list_users(req: Request):
    _require_ui_access(req)
    _require_admin(req)
    users = user_store.list_users(S.USER_DB_PATH)
    out = []
    for u in users:
        out.append({"id": u.id, "username": u.username, "disabled": u.disabled, "admin": getattr(u, "admin", False)})
    return JSONResponse({"users": out})


@router.post("/ui/api/users", include_in_schema=False)
async def ui_api_create_user(req: Request):
    _require_ui_access(req)
    _require_admin(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    username = str(body.get("username") or "").strip()
    password = body.get("password")
    admin_flag = bool(body.get("admin") or False)
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not isinstance(password, str) or not password:
        raise HTTPException(status_code=400, detail="password required")

    try:
        user = user_store.create_user_with_admin(S.USER_DB_PATH, username=username, password=password, admin=admin_flag)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"ok": True, "user": {"id": user.id, "username": user.username, "admin": getattr(user, "admin", False)}})


@router.post("/ui/api/users/bulk", include_in_schema=False)
async def ui_api_bulk_users(req: Request):
    _require_ui_access(req)
    _require_admin(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    action = str(body.get("action") or "").strip().lower()
    users = body.get("users")
    if not action:
        raise HTTPException(status_code=400, detail="action required")
    if not isinstance(users, list) or not users:
        raise HTTPException(status_code=400, detail="users required")

    password = body.get("password")
    confirm = str(body.get("confirm") or "").strip().lower()
    results = []
    for raw in users:
        username = str(raw or "").strip()
        if not username:
            results.append({"username": "", "ok": False, "error": "username required"})
            continue
        try:
            if action == "activate":
                user_store.disable_user(S.USER_DB_PATH, username=username, disabled=False)
            elif action == "deactivate":
                user_store.disable_user(S.USER_DB_PATH, username=username, disabled=True)
            elif action == "admin":
                user_store.set_admin(S.USER_DB_PATH, username=username, admin=True)
            elif action == "non-admin":
                user_store.set_admin(S.USER_DB_PATH, username=username, admin=False)
            elif action == "delete":
                if confirm != "delete":
                    raise ValueError("delete confirmation required")
                user_store.delete_user(S.USER_DB_PATH, username=username)
            elif action == "reset-password":
                if not isinstance(password, str) or not password:
                    raise ValueError("password required")
                user_store.set_password(S.USER_DB_PATH, username=username, password=password)
            else:
                raise ValueError("unknown action")
            results.append({"username": username, "ok": True})
        except ValueError as e:
            # Log the actual exception for debugging, but don't expose to external users
            logger.warning("Bulk user operation failed for username '%s': %s", username, str(e))
            # Do not expose internal exception messages to the client.
            results.append({"username": username, "ok": False, "error": "invalid request"})
    return JSONResponse({"ok": True, "action": action, "results": results})


@router.get("/ui/api/auth/me", include_in_schema=False)
async def ui_auth_me(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": {"id": user.id, "username": user.username, "admin": getattr(user, 'admin', False)}}


@router.get("/ui/api/user/settings", include_in_schema=False)
async def ui_user_settings_get(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"settings": user_store.get_settings(S.USER_DB_PATH, user_id=-1)}
    settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id)
    return {"settings": settings}


@router.put("/ui/api/user/settings", include_in_schema=False)
async def ui_user_settings_put(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    user_store.set_settings(S.USER_DB_PATH, user_id=user.id, settings=settings)
    return {"ok": True}


@router.post("/ui/api/user/password", include_in_schema=False)
async def ui_user_change_password(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    current = body.get("current")
    new = body.get("new")
    if not isinstance(current, str) or not current:
        raise HTTPException(status_code=400, detail="current password required")
    if not isinstance(new, str) or not new:
        raise HTTPException(status_code=400, detail="new password required")

    # Verify current password
    auth = user_store.authenticate(S.USER_DB_PATH, username=user.username, password=current)
    if auth is None:
        raise HTTPException(status_code=401, detail="current password invalid")

    try:
        user_store.set_password(S.USER_DB_PATH, username=user.username, password=new)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/ui/api/conversations", include_in_schema=False)
async def ui_conversation_list(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"conversations": []}
    return {"conversations": user_store.list_conversations(S.USER_DB_PATH, user_id=user.id)}


@router.post("/ui/api/conversations/new", include_in_schema=False)
async def ui_conversation_new(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        convo = ui_conversations.create()
        return {"conversation_id": convo.id}
    convo = user_store.create_conversation(S.USER_DB_PATH, user_id=user.id)
    return {"conversation_id": convo["id"]}


@router.get("/ui/api/conversations/{conversation_id}", include_in_schema=False)
async def ui_conversation_get(req: Request, conversation_id: str) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        convo = ui_conversations.load(conversation_id)
        if convo is None:
            # If the client provided a syntactically safe conversation id (e.g. from
            # localStorage) but the server has no record (e.g. after a cleanup or
            # deploy), create a new empty conversation using the same id so the
            # UI can continue using its stored id rather than receiving a 404.
            if not ui_conversations._is_safe_id(conversation_id):
                raise HTTPException(status_code=404, detail="not found")
            now = int(time.time())
            try:
                convo = ui_conversations.Conversation(id=conversation_id, created=now, updated=now, summary="", messages=[])
                ui_conversations.save(convo)
            except Exception:
                raise HTTPException(status_code=500, detail="failed to create conversation")
        return convo.to_dict()
    convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="not found")
    return convo


@router.post("/ui/api/conversations/{conversation_id}/append", include_in_schema=False)
async def ui_conversation_append(req: Request, conversation_id: str) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    msg = body.get("message")
    if not isinstance(msg, dict):
        raise HTTPException(status_code=400, detail="message must be an object")
    try:
        if user is None:
            convo = ui_conversations.append_message(conversation_id, msg)
            updated = convo.updated
        else:
            convo = user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg=msg)
            updated = convo.get("updated")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to append: {type(e).__name__}: {e}")
    return {"ok": True, "updated": updated}


@router.post("/ui/api/conversations/{conversation_id}/files", include_in_schema=False)
async def ui_conversation_files_upload(
    req: Request, conversation_id: str, files: list[UploadFile] = File(...)
) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if not files:
        raise HTTPException(status_code=400, detail="files required")

    if user is None:
        convo = ui_conversations.load(conversation_id)
        if convo is None:
            if not ui_conversations._is_safe_id(conversation_id):
                raise HTTPException(status_code=404, detail="conversation not found")
            now = int(time.time())
            try:
                convo = ui_conversations.Conversation(id=conversation_id, created=now, updated=now, summary="", messages=[])
                ui_conversations.save(convo)
            except Exception:
                raise HTTPException(status_code=500, detail="conversation not found")
    else:
        convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id)
        if convo is None:
            raise HTTPException(status_code=404, detail="conversation not found")

    saved: list[Dict[str, Any]] = []
    for upload in files:
        try:
            saved.append(await _save_ui_file(upload=upload))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to save file: {type(e).__name__}: {e}")

    return {"files": saved}


@router.get("/ui/images/{name}", include_in_schema=False)
async def ui_image_file(req: Request, name: str):
    _require_ui_access(req)
    user = _require_user(req)

    if not _SAFE_FILE_RE.match(name or ""):
        raise HTTPException(status_code=404, detail="not found")

    img_dir = _ui_image_dir()
    ttl_sec = _ui_image_ttl_sec()
    _ensure_dir(img_dir)
    _cleanup_ui_images(img_dir, ttl_sec=ttl_sec)

    img_dir_real = os.path.realpath(img_dir)
    full = os.path.realpath(os.path.join(img_dir_real, name))
    if os.path.commonpath([img_dir_real, full]) != img_dir_real:
        raise HTTPException(status_code=404, detail="not found")
    try:
        st = os.stat(full)
        if ttl_sec > 0 and (time.time() - float(st.st_mtime)) > float(ttl_sec):
            # Expired; best-effort delete.
            try:
                os.remove(full)
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="expired")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    media_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")

    headers = {"cache-control": "private, max-age=60"}
    return FileResponse(full, media_type=media_type, headers=headers)


@router.get("/ui/files/{name}", include_in_schema=False)
async def ui_uploaded_file(req: Request, name: str):
    _require_ui_access(req)
    _require_user(req)

    if not _SAFE_FILE_RE.match(name or ""):
        raise HTTPException(status_code=404, detail="not found")

    file_dir = _ui_file_dir()
    ttl_sec = _ui_file_ttl_sec()
    _ensure_dir(file_dir)
    _cleanup_ui_files(file_dir, ttl_sec=ttl_sec)

    file_dir_real = os.path.realpath(file_dir)
    full = os.path.realpath(os.path.join(file_dir_real, name))
    if os.path.commonpath([file_dir_real, full]) != file_dir_real:
        raise HTTPException(status_code=404, detail="not found")
    try:
        st = os.stat(full)
        if ttl_sec > 0 and (time.time() - float(st.st_mtime)) > float(ttl_sec):
            try:
                os.remove(full)
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="expired")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    guessed, _ = mimetypes.guess_type(full)
    media_type = guessed or "application/octet-stream"
    headers = {"cache-control": "private, max-age=60"}
    return FileResponse(full, media_type=media_type, headers=headers)


@router.get("/ui/api/models", include_in_schema=False)
async def ui_models(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)

    now = now_unix()
    data: Dict[str, Any] = {"object": "list", "data": []}

    async with _httpx_client(timeout=30) as client:
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = r.json().get("models", [])
            for m in models:
                name = m.get("name")
                if name:
                    data["data"].append({"id": f"ollama:{name}", "object": "model", "created": now, "owned_by": "local"})
        except Exception:
            pass

        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            models = r.json().get("data", [])
            for m in models:
                mid = m.get("id")
                if mid:
                    data["data"].append({"id": f"mlx:{mid}", "object": "model", "created": now, "owned_by": "local"})
        except Exception:
            pass

    # Add convenience backend pseudo-models.
    data["data"].append({"id": "ollama", "object": "model", "created": now, "owned_by": "gateway"})
    data["data"].append({"id": "mlx", "object": "model", "created": now, "owned_by": "gateway"})

    # Add configured aliases so the UI can select stable names (fast/coder/etc).
    aliases = get_aliases()
    for alias_name in sorted(aliases.keys()):
        a = aliases[alias_name]
        item: Dict[str, Any] = {"id": alias_name, "object": "model", "created": now, "owned_by": "gateway"}
        item["backend"] = a.backend
        item["upstream_model"] = a.upstream_model
        if a.context_window:
            item["context_window"] = a.context_window
        if a.tools is not None:
            item["tools"] = a.tools
        if a.max_tokens_cap is not None:
            item["max_tokens_cap"] = a.max_tokens_cap
        if a.temperature_cap is not None:
            item["temperature_cap"] = a.temperature_cap
        data["data"].append(item)

    return data


@router.post("/ui/api/chat", include_in_schema=False)
async def ui_chat(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    model = (body.get("model") or "fast").strip()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    cc = ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=message)],
        stream=False,
    )

    # Prepend per-user profile/system prompt when authenticated so the model
    # personalizes responses while still answering the provided user message.
    try:
        pmsg = _build_profile_system_message(user)
        if pmsg:
            cc.messages = [pmsg] + cc.messages
    except Exception:
        pass

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={k.lower(): v for k, v in req.headers.items()},
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    cc_routed = ChatCompletionRequest(
        model=upstream_model if backend == "mlx" else cc.model,
        messages=cc.messages,
        tools=None,
        tool_choice=None,
        temperature=cc.temperature,
        max_tokens=cc.max_tokens,
        stream=False,
    )

    try:
        resp = await (call_mlx_openai(cc_routed) if backend == "mlx" else call_ollama(cc, upstream_model))
    finally:
        admission.release(backend_class, "chat")

    # Include routing metadata so the UI can display it.
    if isinstance(resp, dict):
        resp.setdefault("_gateway", {})
        if isinstance(resp.get("_gateway"), dict):
            resp["_gateway"].update({"backend": backend, "model": upstream_model, "reason": route.reason})
    return resp


def _coerce_messages(body: dict[str, Any]) -> list[ChatMessage]:
    raw = body.get("messages")
    if isinstance(raw, list) and raw:
        out: list[ChatMessage] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip() or "user"
            content = item.get("content")
            if not isinstance(content, str):
                content = ""
            out.append(ChatMessage(role=role, content=content))
        if out:
            return out

    # Back-compat: single message.
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return [ChatMessage(role="user", content=message.strip())]
    return []


def _build_profile_system_message(user: Optional[user_store.User]) -> ChatMessage | None:
    if user is None:
        return None
    try:
        settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
        profile = settings.get("profile") if isinstance(settings, dict) else None
        if not isinstance(profile, dict):
            return None
        prompt = str(profile.get("system_prompt") or "").strip()
        tone = str(profile.get("tone") or "").strip()
        if not prompt and not tone:
            return None
        parts: list[str] = []
        if prompt:
            parts.append(prompt)
        if tone:
            parts.append(f"Tone guidance: {tone}")
        content = "\n\n".join(parts)
        # Bound the profile content to avoid blowing model context.
        try:
            max_chars = int(getattr(S, "UI_PROFILE_MAX_CHARS", 2000) or 2000)
        except Exception:
            max_chars = 2000
        if len(content) > max_chars:
            content = content[: max_chars - 1] + "…"
        return ChatMessage(role="system", content=content)
    except Exception:
        return None


def _coerce_attachments(raw: Any) -> list[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        url = str(item.get("url") or "").strip()
        if not filename or not url:
            continue
        entry = {
            "filename": filename,
            "url": url,
            "mime": str(item.get("mime") or "").strip(),
            "bytes": item.get("bytes"),
            "sha256": str(item.get("sha256") or "").strip(),
        }
        out.append(entry)
    return out


def _attachments_to_lines(attachments: list[Dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        url = str(item.get("url") or "").strip()
        if not filename or not url:
            continue
        mime = str(item.get("mime") or "").strip()
        size = item.get("bytes")
        bits = []
        if mime:
            bits.append(mime)
        if isinstance(size, int) and size > 0:
            bits.append(f"{size} bytes")
        meta = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {filename}{meta}: {url}")
    return lines


def _conversation_to_chat_messages(convo: ui_conversations.Conversation) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    if convo.summary:
        msgs.append(ChatMessage(role="system", content=f"Conversation summary:\n{convo.summary.strip()}"))

    # Collect textual messages (skip images) and preserve only the last user
    # message as the actual user prompt. All earlier messages are folded into a
    # single system-side "context" message so the model can use them for
    # reference but will not attempt to reply to each one separately.
    items: list[Dict[str, Any]] = []
    for item in convo.messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "image":
            continue
        role = str(item.get("role") or "").strip() or "user"
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            content = ""
        attachments = item.get("attachments")
        attachment_list = _coerce_attachments(attachments)
        if not content.strip() and not attachment_list:
            continue
        items.append({"role": role, "content": content.strip(), "attachments": attachment_list})

    # Find the last user message index
    last_user_idx = -1
    for i in range(len(items) - 1, -1, -1):
        if items[i]["role"] == "user":
            last_user_idx = i
            break

    # Build context lines from all messages except the chosen last user prompt.
    context_lines: list[str] = []
    for i, item in enumerate(items):
        if i == last_user_idx:
            continue
        role = item["role"]
        content = item["content"]
        if content:
            context_lines.append(f"{role}: {content}")
        attachment_lines = _attachments_to_lines(item.get("attachments") or [])
        if attachment_lines:
            context_lines.append(f"{role} attached files:\n" + "\n".join(attachment_lines))

    # Truncate context lines to configured keep size to bound upstream tokens.
    try:
        keep_n = _summary_keep_last_messages()
    except Exception:
        keep_n = 12
    if len(context_lines) > keep_n:
        context_lines = context_lines[-keep_n:]

    if context_lines:
        ctx = "Previous messages (for context only). Do NOT answer these directly:\n" + "\n".join(context_lines)
        msgs.append(ChatMessage(role="system", content=ctx))

    # Append the most recent user message as the sole user prompt the model
    # should answer to. If no user message exists, fall back to the last
    # available message.
    if last_user_idx != -1:
        last_item = items[last_user_idx]
        content = last_item["content"] or ""
        attachment_lines = _attachments_to_lines(last_item.get("attachments") or [])
        if attachment_lines:
            attachment_block = "Attached files:\n" + "\n".join(attachment_lines)
            content = f"{content}\n\n{attachment_block}".strip()
        msgs.append(ChatMessage(role="user", content=content))
    elif items:
        last_item = items[-1]
        content = last_item["content"] or ""
        attachment_lines = _attachments_to_lines(last_item.get("attachments") or [])
        if attachment_lines:
            attachment_block = "Attached files:\n" + "\n".join(attachment_lines)
            content = f"{content}\n\n{attachment_block}".strip()
        msgs.append(ChatMessage(role=last_item["role"], content=content))

    return msgs


async def _stream_ui_chat(
    upstream_gen: Any,
    backend: str,
    upstream_model: str,
    route: Any,
    conversation_id: str,
    user: Any,
    backend_class: str,
    admission: Any,
    pre_events: list | None = None,
):
    try:
        # Emit any pre-collected events from server-side command handling.
        if pre_events:
            for ev in pre_events:
                try:
                    yield sse(ev)
                except Exception:
                    # best-effort: skip malformed pre-events
                    continue

        # Announce routing info first
        yield sse({"type": "route", "backend": backend, "model": upstream_model, "reason": route.reason})

        full_text = ""

        async for chunk in upstream_gen:
            for line in chunk.splitlines():
                if not line.startswith(b"data:"):
                    continue
                data = line[len(b"data:") :].strip()
                if data == b"[DONE]":
                    yield sse({"type": "done"})
                    yield sse_done()
                    return

                try:
                    j = json.loads(data)
                except Exception:
                    continue

                if isinstance(j, dict) and isinstance(j.get("error"), dict):
                    yield sse({"type": "error", "error": j.get("error")})
                    continue

                try:
                    delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                    text = delta.get("content")
                    thinking = delta.get("thinking")
                except Exception:
                    text = None
                    thinking = None

                if isinstance(thinking, str) and thinking:
                    yield sse({"type": "thinking", "thinking": thinking})

                if isinstance(text, str) and text:
                    full_text += text
                    yield sse({"type": "delta", "delta": text})

        # After streaming completes, persist assistant message (if any)
        if conversation_id:
            try:
                if user is None:
                    ui_conversations.append_message(
                        conversation_id,
                        {
                            "role": "assistant",
                            "content": full_text,
                            "backend": backend,
                            "model": upstream_model,
                            "reason": route.reason,
                        },
                    )
                else:
                    user_store.append_message(
                        S.USER_DB_PATH,
                        user_id=user.id,
                        conversation_id=conversation_id,
                        msg={
                            "role": "assistant",
                            "content": full_text,
                            "backend": backend,
                            "model": upstream_model,
                            "reason": route.reason,
                        },
                    )
            except Exception:
                # Best-effort persistence; do not fail the stream on storage errors.
                pass

        # Signal completion to the UI
        yield sse({"type": "done"})
        yield sse_done()
    finally:
        admission.release(backend_class, "chat")


def _conversation_payload_to_chat_messages(convo: Dict[str, Any]) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    summary = str(convo.get("summary") or "").strip()
    if summary:
        msgs.append(ChatMessage(role="system", content=f"Conversation summary:\n{summary}"))

    raw_messages = convo.get("messages")
    if not isinstance(raw_messages, list):
        return msgs

    # Merge consecutive messages of the same role so upstreams see alternating
    # turns rather than many discrete same-role items.
    last_role: str | None = None
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "user"
        if str(item.get("type") or "") == "image":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            content = ""
        attachments = _coerce_attachments(item.get("attachments"))
        attachment_lines = _attachments_to_lines(attachments)
        if attachment_lines:
            attachment_block = "Attached files:\n" + "\n".join(attachment_lines)
            content = f"{content}\n\n{attachment_block}".strip()
        if not content:
            continue

        if last_role is not None and last_role == role and msgs:
            prev = msgs[-1]
            prev.content = (prev.content or "") + "\n" + content
        else:
            msgs.append(ChatMessage(role=role, content=content))
            last_role = role

    # Fold prior messages into a single system context message and present
    # only the last user message as the user prompt to ensure the model directs
    # its reply to the most recent input while still having prior context.
    raw_messages = convo.get("messages")
    items: list[Dict[str, Any]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "image":
            continue
        role = str(item.get("role") or "").strip() or "user"
        content = item.get("content")
        if not isinstance(content, str):
            content = ""
        attachments = _coerce_attachments(item.get("attachments"))
        if not content.strip() and not attachments:
            continue
        items.append({"role": role, "content": content.strip(), "attachments": attachments})

    last_user_idx = -1
    for i in range(len(items) - 1, -1, -1):
        if items[i]["role"] == "user":
            last_user_idx = i
            break

    context_lines: list[str] = []
    for i, item in enumerate(items):
        if i == last_user_idx:
            continue
        role = item["role"]
        content = item["content"]
        if content:
            context_lines.append(f"{role}: {content}")
        attachment_lines = _attachments_to_lines(item.get("attachments") or [])
        if attachment_lines:
            context_lines.append(f"{role} attached files:\n" + "\n".join(attachment_lines))

    try:
        keep_n = _summary_keep_last_messages()
    except Exception:
        keep_n = 12
    if len(context_lines) > keep_n:
        context_lines = context_lines[-keep_n:]

    if context_lines:
        ctx = "Previous messages (for context only). Do NOT answer these directly:\n" + "\n".join(context_lines)
        msgs.append(ChatMessage(role="system", content=ctx))

    if last_user_idx != -1:
        last_item = items[last_user_idx]
        content = last_item["content"] or ""
        attachment_lines = _attachments_to_lines(last_item.get("attachments") or [])
        if attachment_lines:
            attachment_block = "Attached files:\n" + "\n".join(attachment_lines)
            content = f"{content}\n\n{attachment_block}".strip()
        msgs.append(ChatMessage(role="user", content=content))
    elif items:
        last_item = items[-1]
        content = last_item["content"] or ""
        attachment_lines = _attachments_to_lines(last_item.get("attachments") or [])
        if attachment_lines:
            attachment_block = "Attached files:\n" + "\n".join(attachment_lines)
            content = f"{content}\n\n{attachment_block}".strip()
        msgs.append(ChatMessage(role=last_item["role"], content=content))

    return msgs


def _summary_trigger_bytes() -> int:
    try:
        return int(getattr(S, "UI_CHAT_SUMMARY_TRIGGER_BYTES", 0) or 0)
    except Exception:
        return 0


def _summary_keep_last_messages() -> int:
    try:
        return int(getattr(S, "UI_CHAT_SUMMARY_KEEP_LAST_MESSAGES", 12) or 12)
    except Exception:
        return 12


async def _summarize_if_needed(convo: ui_conversations.Conversation) -> ui_conversations.Conversation:
    trigger = _summary_trigger_bytes()
    if trigger <= 0:
        return convo

    # Estimate size based on current stored JSON-ish payload.
    try:
        approx = len(json.dumps(convo.to_dict(), ensure_ascii=False).encode("utf-8"))
    except Exception:
        approx = 0
    if approx <= trigger:
        return convo

    keep_n = max(4, _summary_keep_last_messages())
    tail = convo.messages[-keep_n:]
    head = convo.messages[:-keep_n]
    if not head:
        return convo

    head_text_parts: list[str] = []
    for m in head:
        if not isinstance(m, dict):
            continue
        if str(m.get("type") or "") == "image":
            continue
        role = str(m.get("role") or "").strip() or "user"
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            head_text_parts.append(f"{role}: {content.strip()}")
        attachment_lines = _attachments_to_lines(_coerce_attachments(m.get("attachments")))
        if attachment_lines:
            head_text_parts.append(f"{role} attached files:\n" + "\n".join(attachment_lines))

    if not head_text_parts:
        convo.messages = tail
        convo.updated = int(time.time())
        ui_conversations.save(convo)
        return convo

    summarizer_model = "long"  # prefer long-context alias if present
    summary_prompt = (
        "Summarize the conversation so far for future context. "
        "Preserve user preferences, goals, key facts, constraints, decisions, and open questions. "
        "Do not include private reasoning or chain-of-thought. Output concise bullet points.\n\n"
        + "\n".join(head_text_parts)
    )

    cc_sum = ChatCompletionRequest(
        model=summarizer_model,
        messages=[ChatMessage(role="user", content=summary_prompt)],
        stream=False,
    )

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc_sum.model,
        headers={},
        messages=[m.model_dump(exclude_none=True) for m in cc_sum.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")
    cc_sum_routed = ChatCompletionRequest(
        model=upstream_model if backend == "mlx" else cc_sum.model,
        messages=cc_sum.messages,
        stream=False,
    )
    try:
        resp = await (call_mlx_openai(cc_sum_routed) if backend == "mlx" else call_ollama(cc_sum, upstream_model))
    finally:
        admission.release(backend_class, "chat")
    text = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    if not isinstance(text, str):
        text = ""

    prior = (convo.summary or "").strip()
    merged = (prior + "\n" + text.strip()).strip() if prior and text.strip() else (text.strip() or prior)
    convo.summary = merged
    convo.messages = tail
    convo.updated = int(time.time())
    ui_conversations.save(convo)
    return convo


@router.post("/ui/api/chat_stream", include_in_schema=False)
async def ui_chat_stream(req: Request):
    """Tokenless SSE stream for the browser UI.

    Emits gateway status events (routing/backend/model), optional thinking snippets,
    and then streamed text deltas. This intentionally does NOT expose hidden
    chain-of-thought; it only streams assistant-visible text and gateway metadata.
    """

    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    model = (body.get("model") or "fast").strip()
    conversation_id = str(body.get("conversation_id") or "").strip()
    message_text = body.get("message")
    attachments = _coerce_attachments(body.get("attachments"))

    # Prefer server-side conversation history if a conversation_id is provided.
    if conversation_id:
        if user is None:
            convo = ui_conversations.load(conversation_id)
            if convo is None:
                # If the client sent a safe but unknown conversation id (for
                # example left in localStorage from before a cleanup), create
                # an empty conversation using the same id so the UI can keep
                # using its stored id instead of getting a 404.
                if not ui_conversations._is_safe_id(conversation_id):
                    raise HTTPException(status_code=404, detail="conversation not found")
                now = int(time.time())
                try:
                    convo = ui_conversations.Conversation(id=conversation_id, created=now, updated=now, summary="", messages=[])
                    ui_conversations.save(convo)
                except Exception:
                    raise HTTPException(status_code=500, detail="conversation not found")

            if (isinstance(message_text, str) and message_text.strip()) or attachments:
                try:
                    payload: Dict[str, Any] = {"role": "user", "content": (message_text or "").strip()}
                    if attachments:
                        payload["attachments"] = attachments
                    ui_conversations.append_message(conversation_id, payload)
                    convo = ui_conversations.load(conversation_id) or convo
                except Exception:
                    pass

            # Best-effort summarization/pruning.
            try:
                convo = await _summarize_if_needed(convo)
            except Exception:
                pass

            messages = _conversation_to_chat_messages(convo)
        else:
            convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id)
            if convo is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            if (isinstance(message_text, str) and message_text.strip()) or attachments:
                try:
                    user_store.append_message(
                        S.USER_DB_PATH,
                        user_id=user.id,
                        conversation_id=conversation_id,
                        msg={"role": "user", "content": (message_text or "").strip(), "attachments": attachments or None},
                    )
                    convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id) or convo
                except Exception:
                    pass
            messages = _conversation_payload_to_chat_messages(convo)
    else:
        messages = _coerce_messages(body)

    if not messages:
        raise HTTPException(status_code=400, detail="messages required")
    # Prepend per-user profile/system prompt when authenticated so the model
    # personalizes responses while still answering the most recent user input.
    try:
        pmsg = _build_profile_system_message(user)
        if pmsg:
            messages = [pmsg] + messages
    except Exception:
        pass
    
    # Collect pre-stream events produced by server-side command handling.
    pre_events: list[dict] = []

    # Server-side command handling: if the user sent a single leading slash-command
    # like `/image`, `/music`, or `/speech`, invoke the appropriate backend and
    # emit a short SSE update with the backend result before continuing to the
    # normal chat model routing. This ensures slash-commands always surface the
    # backend output even if the chat model responds differently.
    try:
        if isinstance(message_text, str) and message_text and len(messages) == 1 and (messages[0].role or "") == "user":
            cmd = message_text.strip()
            low = cmd.lower()
            # /image
            if low == "/image" or low.startswith("/image "):
                prompt = cmd.replace("/image", "", 1).strip()
                try:
                    # Announce backend work to the UI
                    pre_events.append({"type": "thinking", "thinking": "Generating image…"})
                    resp = await generate_images(prompt=prompt or "", size="1024x1024", n=1, model=None, options=None, response_format="url")
                    url = None
                    if isinstance(resp, dict):
                        if isinstance(resp.get("data"), list) and resp["data"]:
                            first = resp["data"][0]
                            if isinstance(first, dict) and isinstance(first.get("url"), str) and first.get("url").strip():
                                url = first.get("url").strip()
                            elif isinstance(first, dict) and isinstance(first.get("b64_json"), str) and first.get("b64_json").strip():
                                # save base64 to UI image cache
                                try:
                                    url, _, _ = _save_ui_image(b64=first.get("b64_json"), mime_hint=resp.get("_gateway", {}).get("mime", "image/png"))
                                except Exception:
                                    url = None
                    if url:
                        pre_events.append({"type": "delta", "delta": f"[Image] {url}"})
                        # persist to conversation if present
                        try:
                            if conversation_id:
                                if user is None:
                                    ui_conversations.append_message(conversation_id, {"role": "assistant", "type": "image", "url": url})
                                else:
                                    user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg={"role": "assistant", "type": "image", "url": url})
                        except Exception:
                            pass
                    else:
                        pre_events.append({"type": "delta", "delta": "[Image] generation returned no usable URL"})
                except Exception as e:
                    pre_events.append({"type": "delta", "delta": f"[Image] generation failed: {type(e).__name__}: {e}"})

            # /music
            elif low == "/music" or low.startswith("/music "):
                prompt = cmd.replace("/music", "", 1).strip()
                try:
                    pre_events.append({"type": "thinking", "thinking": "Generating music…"})
                    from app.music_backend import generate_music

                    out = await generate_music(backend_class=getattr(S, "MUSIC_BACKEND_CLASS", "heartmula_music"), body={"prompt": prompt})
                    url = out.get("audio_url") if isinstance(out, dict) else None
                    if url:
                        pre_events.append({"type": "delta", "delta": f"[Music] {url}"})
                        try:
                            if conversation_id:
                                if user is None:
                                    ui_conversations.append_message(conversation_id, {"role": "assistant", "type": "music", "url": url})
                                else:
                                    user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg={"role": "assistant", "type": "music", "url": url})
                        except Exception:
                            pass
                    else:
                        pre_events.append({"type": "delta", "delta": "[Music] generation returned no audio URL"})
                except Exception as e:
                    pre_events.append({"type": "delta", "delta": f"[Music] generation failed: {type(e).__name__}: {e}"})

            # /speech or /tts
            elif low == "/speech" or low.startswith("/speech ") or low.startswith("/tts"):
                prompt = cmd.replace("/speech", "", 1).replace("/tts", "", 1).strip()
                try:
                    pre_events.append({"type": "thinking", "thinking": "Synthesizing speech…"})
                    backend_class = (getattr(S, "TTS_BACKEND_CLASS", "") or "").strip() or "pocket_tts"
                    check_backend_ready(backend_class, route_kind="tts")
                    await check_capability(backend_class, "tts")
                    admission = get_admission_controller()
                    await admission.acquire(backend_class, "tts")
                    try:
                        from app.tts_backend import generate_tts

                        # Include authenticated user's preferred TTS voice if available
                        tts_body = {"text": prompt}
                        try:
                            if user is not None:
                                settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
                                voice = None
                                try:
                                    voice = (settings.get("tts") or {}).get("voice") if isinstance(settings, dict) else None
                                except Exception:
                                    voice = None
                                if isinstance(voice, str) and voice:
                                    tts_body["voice"] = voice
                        except Exception:
                            # ignore settings lookup failures and fallback to default
                            pass

                        res = await generate_tts(backend_class=backend_class, body=tts_body)
                    finally:
                        admission.release(backend_class, "tts")

                    audio_url = None
                    # If backend returned a dict containing an audio_url, use it.
                    if isinstance(res, dict):
                        audio_url = res.get("audio_url")
                    else:
                        # TtsResult: if raw bytes present, try to cache and expose a UI URL.
                        try:
                            raw = getattr(res, "audio", None)
                            ctype = getattr(res, "content_type", "audio/wav")
                            if raw:
                                url, _ = _save_ui_audio(audio_bytes=raw, mime_hint=ctype)
                                audio_url = url
                        except Exception:
                            audio_url = None

                    if audio_url:
                        # Emit structured audio event for the UI to play.
                        ctype_local = None
                        try:
                            ctype_local = ctype  # defined when we cached raw bytes
                        except Exception:
                            ctype_local = None
                        fname = os.path.basename(audio_url) if isinstance(audio_url, str) else None
                        ev = {"type": "audio", "url": audio_url}
                        if ctype_local:
                            ev["content_type"] = ctype_local
                        if fname:
                            ev["filename"] = fname
                        ev["meta"] = {"backend": backend_class}
                        pre_events.append(ev)
                        try:
                            if conversation_id:
                                if user is None:
                                    ui_conversations.append_message(conversation_id, {"role": "assistant", "type": "audio", "url": audio_url})
                                else:
                                    user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg={"role": "assistant", "type": "audio", "url": audio_url})
                        except Exception:
                            pass
                    else:
                        pre_events.append({"type": "delta", "delta": "[Speech] synthesized audio available in TTS UI or returned inline."})
                except Exception as e:
                    pre_events.append({"type": "delta", "delta": f"[Speech] synthesis failed: {type(e).__name__}: {e}"})

            # /scan — OCR an image URL via the LightOnOCR shim
            elif low == "/scan" or low.startswith("/scan "):
                image_url = cmd.replace("/scan", "", 1).strip()
                if not image_url:
                    pre_events.append({"type": "delta", "delta": "[Scan] usage: /scan <image_url>"})
                else:
                    try:
                        pre_events.append({"type": "thinking", "thinking": "Scanning image…"})
                        import httpx
                        import json as _json

                        base = (getattr(S, "LIGHTON_OCR_API_BASE_URL", "") or os.environ.get("LIGHTON_OCR_API_BASE_URL") or "").strip().rstrip("/")
                        if not base:
                            pre_events.append({"type": "delta", "delta": "[Scan] LightOnOCR is not configured (set LIGHTON_OCR_API_BASE_URL in the gateway env)."})
                        else:
                            timeout_sec = float(getattr(S, "LIGHTON_OCR_TIMEOUT_SEC", 120) or 120)
                            timeout = httpx.Timeout(connect=10.0, read=timeout_sec, write=10.0, pool=10.0)
                            async with httpx.AsyncClient(timeout=timeout) as client:
                                resp = await client.post(f"{base}/v1/ocr", json={"image_url": image_url})
                            if resp.status_code >= 400:
                                pre_events.append({"type": "delta", "delta": f"[Scan] failed: HTTP {resp.status_code}: {resp.text}"})
                            else:
                                try:
                                    data = resp.json()
                                except Exception:
                                    data = {"raw": resp.text}

                                # Best-effort text extraction from common shapes
                                text = None
                                if isinstance(data, dict):
                                    if isinstance(data.get("text"), str) and data.get("text").strip():
                                        text = data.get("text").strip()
                                    elif isinstance(data.get("data"), list):
                                        parts = []
                                        for item in data.get("data"):
                                            if isinstance(item, dict):
                                                t = item.get("text") or item.get("raw_text") or item.get("transcript")
                                                if isinstance(t, str) and t.strip():
                                                    parts.append(t.strip())
                                                elif isinstance(item.get("lines"), list):
                                                    for ln in item.get("lines"):
                                                        if isinstance(ln, dict) and isinstance(ln.get("text"), str):
                                                            parts.append(ln.get("text"))
                                        if parts:
                                            text = "\n".join(parts)

                                if not text:
                                    text = _json.dumps(data, ensure_ascii=False)[:2000]

                                pre_events.append({"type": "delta", "delta": f"[Scan] {text}"})
                                try:
                                    if conversation_id:
                                        if user is None:
                                            ui_conversations.append_message(conversation_id, {"role": "assistant", "type": "scan", "text": text, "image_url": image_url})
                                        else:
                                            user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg={"role": "assistant", "type": "scan", "text": text, "image_url": image_url})
                                except Exception:
                                    pass
                    except Exception as e:
                        pre_events.append({"type": "delta", "delta": f"[Scan] failed: {type(e).__name__}: {e}"})
    except Exception:
        # best-effort only; do not fail the chat stream on command-handling errors
        pass
    cc = ChatCompletionRequest(
        model=model,
        messages=messages,
        stream=True,
    )

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={k.lower(): v for k, v in req.headers.items()},
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    try:
        cc_routed = ChatCompletionRequest(
            model=upstream_model if backend == "mlx" else cc.model,
            messages=cc.messages,
            tools=None,
            tool_choice=None,
            temperature=cc.temperature,
            max_tokens=cc.max_tokens,
            stream=True,
        )

        if backend == "mlx":
            payload = cc_routed.model_dump(exclude_none=True)
            payload["model"] = upstream_model
            payload["stream"] = True
            upstream_gen = stream_mlx_openai_chat(payload)
        else:
            upstream_gen = stream_ollama_chat_as_openai(cc_routed, upstream_model)
    except Exception:
        admission.release(backend_class, "chat")
        raise

    out = StreamingResponse(
        _stream_ui_chat(
            upstream_gen=upstream_gen,
            backend=backend,
            upstream_model=upstream_model,
            route=route,
            conversation_id=conversation_id,
            user=user,
            backend_class=backend_class,
            admission=admission,
            pre_events=pre_events,
        ),
        media_type="text/event-stream",
    )
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = upstream_model
    out.headers["X-Router-Reason"] = route.reason
    return out


@router.get("/ui/api/backend_status", include_in_schema=False)
async def ui_api_backend_status(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    registry = get_registry()
    checker = get_health_checker()
    aliases = get_aliases()
    alias_map: Dict[str, List[Dict[str, str]]] = {}
    for alias_name, target_backend in registry.legacy_mapping.items():
        if not isinstance(alias_name, str) or not isinstance(target_backend, str):
            continue
        alias_map.setdefault(target_backend, []).append(
            {"name": alias_name, "target": target_backend, "kind": "legacy"}
        )
    for alias_name, alias in aliases.items():
        resolved_backend = registry.resolve_backend_class(alias.backend)
        alias_map.setdefault(resolved_backend, []).append(
            {
                "name": alias_name,
                "target": f"{alias.backend}:{alias.upstream_model}",
                "kind": "model",
            }
        )
    backends = []
    for backend_class, config in registry.backends.items():
        entry: Dict[str, Any] = {
            "backend_class": backend_class,
            "capabilities": list(config.supported_capabilities),
        }
        alias_entries = alias_map.get(backend_class)
        if alias_entries:
            entry["aliases"] = sorted(alias_entries, key=lambda item: item.get("name") or "")
        status = checker.get_status(backend_class)
        if status is not None:
            entry.update(
                {
                    "healthy": status.is_healthy,
                    "ready": status.is_ready,
                    "last_check": status.last_check,
                    "error": status.error,
                }
            )
        backends.append(entry)
    backends.sort(key=lambda item: item.get("backend_class") or "")
    return {"generated_at": time.time(), "backends": backends}


@router.post("/ui/api/image", include_in_schema=False)
async def ui_image(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")

    size = str(body.get("size") or "1024x1024")
    n = int(body.get("n") or 1)
    model = body.get("model")

    options = {}
    for k in [
        "seed",
        "steps",
        "num_inference_steps",
        "guidance",
        "guidance_scale",
        "cfg_scale",
        "negative_prompt",
        "sampler",
        "scheduler",
        "style",
        "quality",
    ]:
        if k in body:
            options[k] = body.get(k)
    if not options:
        options = None

    try:
        resp = await generate_images(
            prompt=prompt,
            size=size,
            n=n,
            model=str(model) if isinstance(model, str) and model.strip() else None,
            options=options,
        )

        # Prefer short-lived URLs for the browser (avoids huge data: URIs and broken rendering).
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            gw = resp.get("_gateway") if isinstance(resp.get("_gateway"), dict) else {}
            mime = (gw.get("mime") or "image/png") if isinstance(gw, dict) else "image/png"
            ttl_sec = _ui_image_ttl_sec()

            out_items: list[dict[str, Any]] = []
            first_sha256: str | None = None
            first_mime: str | None = None
            for item in resp.get("data")[:n]:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json")
                if isinstance(b64, str) and b64.strip():
                    url, mime_used, sha256 = _save_ui_image(b64=b64, mime_hint=str(mime))
                    out_items.append({"url": url})
                    mime = mime_used
                    if first_sha256 is None:
                        first_sha256 = sha256
                        first_mime = mime_used

            if out_items:
                resp["data"] = out_items
                resp.setdefault("_gateway", {})
                if isinstance(resp.get("_gateway"), dict):
                    resp["_gateway"].update({"mime": mime, "ui_cache": True, "ttl_sec": ttl_sec})
                    if first_sha256:
                        resp["_gateway"].update({"ui_image_sha256": first_sha256})
                    if first_mime:
                        resp["_gateway"].update({"ui_image_mime": first_mime})

        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")


@router.post("/ui/api/scan", include_in_schema=False)
async def ui_scan(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        raise HTTPException(status_code=400, detail="image_url required")

    try:
        import httpx
        import os

        base = (getattr(S, "LIGHTON_OCR_API_BASE_URL", "") or os.environ.get("LIGHTON_OCR_API_BASE_URL") or "").strip().rstrip("/")
        if not base:
            raise HTTPException(
                status_code=503,
                detail="LightOnOCR is not configured. Set LIGHTON_OCR_API_BASE_URL in the gateway env (not just in UI).",
            )
        timeout_sec = float(getattr(S, "LIGHTON_OCR_TIMEOUT_SEC", 120) or 120)
        timeout = httpx.Timeout(connect=10.0, read=timeout_sec, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/v1/ocr", json={"image_url": image_url})
            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise HTTPException(status_code=resp.status_code, detail=detail)
            try:
                return resp.json()
            except Exception:
                raise HTTPException(status_code=502, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ocr backend error: {type(e).__name__}: {e}")
