from __future__ import annotations

import json
import secrets
import time
import hashlib
from typing import Any, Dict, Optional

from app import user_store
from app.config import S, logger
from app.httpx_client import httpx_client


def notifications_enabled() -> bool:
    return bool(getattr(S, "TELEGRAM_NOTIFY_ENABLED", True)) and bool(str(getattr(S, "TELEGRAM_TOKEN", "") or "").strip())


def _normalize_username(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    return raw


def _coerce_chat_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("-") and raw[1:].isdigit():
        return raw
    if raw.isdigit():
        return raw
    return ""


def _link_code_ttl_sec() -> int:
    return 15 * 60


def _normalize_link_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return "".join(ch for ch in raw if ch.isalnum())


def _link_code_hash(value: str) -> str:
    return hashlib.sha256(_normalize_link_code(value).encode("utf-8")).hexdigest()


def _telegram_link_state(settings: Dict[str, Any]) -> Dict[str, Any]:
    telegram = settings.get("telegram") if isinstance(settings, dict) else None
    telegram = telegram if isinstance(telegram, dict) else {}
    link = telegram.get("link") if isinstance(telegram.get("link"), dict) else {}
    return link if isinstance(link, dict) else {}


def create_link_code(*, user_id: int) -> Dict[str, Any]:
    settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user_id)) or {}
    telegram = settings.get("telegram") if isinstance(settings.get("telegram"), dict) else {}
    telegram = dict(telegram)
    link = _telegram_link_state(settings)
    now_ts = int(time.time())
    code = secrets.token_hex(4)
    link.update(
        {
            "pending_code_hash": _link_code_hash(code),
            "pending_created_ts": now_ts,
            "pending_expires_ts": now_ts + _link_code_ttl_sec(),
        }
    )
    telegram["link"] = link
    settings["telegram"] = telegram
    user_store.set_settings(S.USER_DB_PATH, user_id=int(user_id), settings=settings)
    return {
        "code": code,
        "expires_ts": int(link.get("pending_expires_ts") or 0),
        "command": f"/link {code}",
    }


def redeem_link_code(*, code: str, chat_id: Any, username: Any = None, telegram_user_id: Any = None, chat_type: Any = None) -> Dict[str, Any]:
    normalized = _normalize_link_code(code)
    if not normalized:
        return {"ok": False, "error": "code_required"}
    target_chat_id = _coerce_chat_id(chat_id)
    if not target_chat_id:
        return {"ok": False, "error": "chat_id_invalid"}

    code_hash = _link_code_hash(normalized)
    now_ts = int(time.time())
    expired_match = False
    for user in user_store.list_users(S.USER_DB_PATH):
        try:
            settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user.id)) or {}
        except Exception:
            continue
        telegram = settings.get("telegram") if isinstance(settings.get("telegram"), dict) else {}
        telegram = dict(telegram)
        link = _telegram_link_state(settings)
        pending_hash = str(link.get("pending_code_hash") or "").strip()
        if not pending_hash:
            continue
        if not secrets.compare_digest(pending_hash, code_hash):
            continue
        expires_ts = int(link.get("pending_expires_ts") or 0)
        if expires_ts and expires_ts < now_ts:
            expired_match = True
            continue
        normalized_username = _normalize_username(username)
        telegram["chat_id"] = target_chat_id
        if normalized_username:
            telegram["username"] = normalized_username
        link.pop("pending_code_hash", None)
        link.pop("pending_created_ts", None)
        link.pop("pending_expires_ts", None)
        link["linked_ts"] = now_ts
        link["telegram_user_id"] = str(telegram_user_id or "").strip()
        link["chat_type"] = str(chat_type or "").strip()
        link["linked_chat_id"] = target_chat_id
        if normalized_username:
            link["linked_username"] = normalized_username
        telegram["link"] = link
        settings["telegram"] = telegram
        user_store.set_settings(S.USER_DB_PATH, user_id=int(user.id), settings=settings)
        return {
            "ok": True,
            "user_id": int(user.id),
            "username": str(user.username or ""),
            "chat_id": target_chat_id,
            "telegram_username": normalized_username,
        }
    return {"ok": False, "error": "code_expired" if expired_match else "code_invalid"}


def resolve_linked_nexus_user(*, telegram_user_id: Any = None, chat_id: Any = None) -> Optional[Dict[str, Any]]:
    """Resolve a Telegram identity to its linked Nexus user.

    Numeric Telegram user IDs are authoritative. A private-chat ID is only used
    as a compatibility fallback for links created before ``telegram_user_id``
    was recorded. Usernames and display names are deliberately ignored.
    """

    target_user_id = str(telegram_user_id or "").strip()
    target_chat_id = _coerce_chat_id(chat_id)
    fallback: Optional[Dict[str, Any]] = None
    for user in user_store.list_users(S.USER_DB_PATH):
        if user.disabled:
            continue
        try:
            settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user.id)) or {}
        except Exception:
            continue
        link = _telegram_link_state(settings)
        linked_user_id = str(link.get("telegram_user_id") or "").strip()
        linked_chat_id = _coerce_chat_id(link.get("linked_chat_id"))
        result = {
            "user_id": int(user.id),
            "username": str(user.username or ""),
            "telegram_user_id": linked_user_id,
            "chat_id": linked_chat_id,
        }
        if target_user_id and linked_user_id and secrets.compare_digest(target_user_id, linked_user_id):
            return result
        if target_chat_id and linked_chat_id and secrets.compare_digest(target_chat_id, linked_chat_id):
            fallback = result
    return fallback


def linked_telegram_identity_for_nexus_user(*, user_id: int) -> Optional[Dict[str, str]]:
    """Return immutable linked Telegram IDs for an authenticated Nexus user."""

    try:
        settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user_id)) or {}
    except Exception:
        return None
    link = _telegram_link_state(settings)
    telegram_user_id = str(link.get("telegram_user_id") or "").strip()
    chat_id = _coerce_chat_id(link.get("linked_chat_id"))
    if not telegram_user_id and not chat_id:
        return None
    return {
        "telegram_user_id": telegram_user_id,
        "chat_id": chat_id,
    }


def workspace_url(task_id: str) -> str:
    public_base = str(getattr(S, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    task = str(task_id or "").strip()
    if not public_base or not task:
        return ""
    return f"{public_base}/ui/coding?task_id={task}"


def _default_app_preferences() -> Dict[str, Dict[str, Any]]:
    return {
        "coding": {
            "enabled": True,
            "notify_on_attention": True,
            "notify_on_recovery": True,
            "notify_on_noteworthy": True,
        },
        "image": {
            "enabled": False,
            "notify_on_complete": False,
        },
        "music": {
            "enabled": False,
            "notify_on_complete": False,
        },
        "video": {
            "enabled": False,
            "notify_on_complete": False,
        },
    }


def _app_preferences(telegram: Dict[str, Any], app: str) -> Dict[str, Any]:
    defaults = _default_app_preferences().get(app, {"enabled": False})
    out = dict(defaults)
    apps = telegram.get("apps") if isinstance(telegram.get("apps"), dict) else {}
    app_settings = apps.get(app) if isinstance(apps.get(app), dict) else {}
    if app == "coding":
        if "notify_on_attention" in telegram and "notify_on_attention" not in app_settings:
            out["notify_on_attention"] = bool(telegram.get("notify_on_attention"))
        if "notify_on_recovery" in telegram and "notify_on_recovery" not in app_settings:
            out["notify_on_recovery"] = bool(telegram.get("notify_on_recovery"))
        if "notify_on_noteworthy" in telegram and "notify_on_noteworthy" not in app_settings:
            out["notify_on_noteworthy"] = bool(telegram.get("notify_on_noteworthy"))
    for key, value in app_settings.items():
        out[str(key)] = value
    return out


def resolve_notification_target(*, user_id: Any = None, owner_username: Any = None, app: str = "coding") -> Dict[str, Any]:
    target_user_id: Optional[int] = None
    try:
        if user_id is not None:
            target_user_id = int(user_id)
    except Exception:
        target_user_id = None

    owner = str(owner_username or "").strip().lower()
    if target_user_id is None and owner:
        try:
            for user in user_store.list_users(S.USER_DB_PATH):
                if str(user.username or "").strip().lower() == owner:
                    target_user_id = int(user.id)
                    break
        except Exception:
            target_user_id = None

    if target_user_id is None:
        return {
            "enabled": False,
            "reason": "no_user",
            "user_id": None,
            "chat_id": "",
            "mention_username": "",
            "notify_on_attention": False,
            "notify_on_recovery": False,
        }

    try:
        settings = user_store.get_settings(S.USER_DB_PATH, user_id=target_user_id) or {}
    except Exception as exc:
        logger.warning("telegram notifications: failed reading settings for user_id=%s error=%s", target_user_id, exc)
        settings = {}

    telegram = settings.get("telegram") if isinstance(settings, dict) else None
    telegram = telegram if isinstance(telegram, dict) else {}
    chat_id = _coerce_chat_id(telegram.get("chat_id"))
    mention_username = _normalize_username(telegram.get("username") or owner)
    app_name = str(app or "coding").strip().lower() or "coding"
    app_preferences = _app_preferences(telegram, app_name)
    globally_enabled = bool(telegram.get("notifications_enabled"))
    enabled = globally_enabled and bool(chat_id) and bool(app_preferences.get("enabled", False))
    return {
        "enabled": enabled,
        "reason": "ok" if enabled else ("chat_id_missing" if globally_enabled else "disabled"),
        "user_id": target_user_id,
        "chat_id": chat_id,
        "mention_username": mention_username,
        "app": app_name,
        "notify_on_attention": bool(app_preferences.get("notify_on_attention", True)),
        "notify_on_recovery": bool(app_preferences.get("notify_on_recovery", True)),
        "notify_on_noteworthy": bool(app_preferences.get("notify_on_noteworthy", True)),
        "notify_on_complete": bool(app_preferences.get("notify_on_complete", False)),
    }


def render_coding_workspace_notification(
    *,
    item: Dict[str, Any],
    event_kind: str,
    mention_username: str = "",
    action: Optional[Dict[str, Any]] = None,
    note: str = "",
    severity: str = "",
) -> str:
    task_id = str(item.get("id") or "").strip()
    owner = str(item.get("owner") or "").strip()
    status = str(item.get("status") or "").strip() or "unknown"
    agent = item.get("agent") if isinstance(item.get("agent"), dict) else {}
    agent_status = str(agent.get("status") or "").strip() or "unknown"
    recommended = str(item.get("recommended_action") or "").strip()
    attention = item.get("attention") if isinstance(item.get("attention"), list) else []
    reasons = ", ".join(str(part) for part in attention if str(part).strip())
    mention = f"@{mention_username} " if mention_username else ""
    url = workspace_url(task_id)

    lines = []
    if event_kind == "auto_resume":
        lines.append(f"{mention}Nexus Sentinel auto-resumed a coding workspace.")
    elif event_kind == "noteworthy":
        lines.append(f"{mention}Nexus Sentinel flagged a noteworthy coding workspace update.")
    else:
        lines.append(f"{mention}A coding workspace needs attention.")
    lines.append(f"Workspace: {task_id}")
    if owner:
        lines.append(f"Owner: {owner}")
    lines.append(f"Workspace status: {status}")
    lines.append(f"Agent status: {agent_status}")
    if reasons:
        lines.append(f"Attention: {reasons}")
    if recommended:
        lines.append(f"Recommended action: {recommended}")
    if action and isinstance(action, dict):
        previous = str(action.get("previous_status") or "").strip()
        next_status = str(action.get("agent_status") or "").strip()
        if previous or next_status:
            lines.append(f"Supervisor action: {previous or 'unknown'} -> {next_status or 'unknown'}")
    if severity:
        lines.append(f"Severity: {severity}")
    if note:
        lines.append(f"Update: {note}")
    if url:
        lines.append(f"Open: {url}")
    return "\n".join(line for line in lines if line)


async def send_message(*, chat_id: str, text: str, disable_notification: bool = False) -> Dict[str, Any]:
    token = str(getattr(S, "TELEGRAM_TOKEN", "") or "").strip()
    if not notifications_enabled() or not token:
        return {"ok": False, "error": "telegram_not_configured"}
    target = _coerce_chat_id(chat_id)
    if not target:
        return {"ok": False, "error": "chat_id_invalid"}
    body = {
        "chat_id": target,
        "text": str(text or "")[:4096],
        "disable_notification": bool(disable_notification),
        "disable_web_page_preview": True,
    }
    timeout = max(1.0, float(getattr(S, "TELEGRAM_NOTIFY_TIMEOUT_SEC", 10.0) or 10.0))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx_client(timeout=timeout) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = json.dumps(resp.json(), ensure_ascii=False)
            except Exception:
                detail = (resp.text or "")[:500]
            logger.warning("telegram notifications: sendMessage failed status=%s detail=%s", resp.status_code, detail)
            return {"ok": False, "error": f"http_{resp.status_code}", "detail": detail}
        payload = resp.json()
        return {"ok": True, "payload": payload}
    except Exception as exc:
        logger.warning("telegram notifications: sendMessage exception chat_id=%s error=%s", target, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
