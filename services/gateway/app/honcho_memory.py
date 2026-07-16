from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import httpx

from app.config import logger
from app import telegram_notifications


WORKSPACE_ID = "nexus"
FLEET_OBSERVER_ID = "nexus_telegram_fleet"
FLEET_OBSERVER_KEY = "nexus:telegram:fleet"
_MAINTENANCE_TASK: Optional[asyncio.Task] = None
_MAINTENANCE_STOP = asyncio.Event()
_MAINTENANCE_LOCK = asyncio.Lock()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _truthy(os.getenv("HONCHO_MEMORY_ENABLED", "false"))


def configured() -> bool:
    return bool(_base_url() and _token())


def status() -> Dict[str, Any]:
    active = enabled() and configured()
    reason = ""
    if not enabled():
        reason = "disabled"
    elif not _base_url():
        reason = "base_url_missing"
    elif not _token():
        reason = "workspace_token_missing"
    return {"enabled": active, "configured": configured(), "reason": reason}


async def health_status() -> Dict[str, Any]:
    state = status()
    if not state["enabled"]:
        return state
    try:
        await _ensure_workspace()
    except httpx.HTTPStatusError as exc:
        return {
            "enabled": False,
            "configured": True,
            "reason": f"honcho_http_{exc.response.status_code}",
        }
    except (httpx.RequestError, RuntimeError):
        return {"enabled": False, "configured": True, "reason": "honcho_unavailable"}
    return state


def _base_url() -> str:
    return str(os.getenv("HONCHO_BASE_URL", "")).strip().rstrip("/")


def _token() -> str:
    return str(os.getenv("HONCHO_WORKSPACE_TOKEN", "")).strip()


def _api_prefix() -> str:
    raw = str(os.getenv("HONCHO_API_PREFIX", "/v3")).strip()
    if not raw:
        return ""
    return "/" + raw.strip("/")


def _workspace_id() -> str:
    raw = str(os.getenv("HONCHO_WORKSPACE_ID", WORKSPACE_ID)).strip()
    return raw or WORKSPACE_ID


def _timeout() -> float:
    try:
        return max(1.0, float(os.getenv("HONCHO_TIMEOUT_SEC", "10") or 10))
    except Exception:
        return 10.0


def _registry_path() -> Path:
    return Path(os.getenv("HONCHO_MEMORY_REGISTRY_PATH", "/var/lib/gateway/data/honcho_memory.sqlite"))


def _export_dir() -> Path:
    return Path(os.getenv("HONCHO_MEMORY_EXPORT_DIR", "/var/lib/gateway/data/honcho_exports"))


def _retention_seconds(chat_type: str) -> int:
    key = "HONCHO_GROUP_RAW_RETENTION_DAYS" if chat_type in {"group", "supergroup", "channel"} else "HONCHO_PRIVATE_RAW_RETENTION_DAYS"
    default = 90 if key.startswith("HONCHO_GROUP") else 180
    try:
        days = max(1, int(os.getenv(key, str(default)) or default))
    except Exception:
        days = default
    return days * 86400


def _export_retention_seconds() -> int:
    try:
        return max(1, int(os.getenv("HONCHO_EXPORT_RETENTION_DAYS", "7") or 7)) * 86400
    except Exception:
        return 7 * 86400


def _audit_retention_seconds() -> int:
    try:
        return max(1, int(os.getenv("HONCHO_AUDIT_RETENTION_DAYS", "365") or 365)) * 86400
    except Exception:
        return 365 * 86400


def _maintenance_interval() -> int:
    try:
        return max(60, int(os.getenv("HONCHO_MAINTENANCE_INTERVAL_SEC", "3600") or 3600))
    except Exception:
        return 3600


def _connect() -> sqlite3.Connection:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_sessions (
                id TEXT PRIMARY KEY,
                honcho_session_id TEXT NOT NULL UNIQUE,
                owner_user_id INTEGER,
                owner_key TEXT NOT NULL,
                participant_key TEXT NOT NULL,
                partition_key TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                telegram_message_id TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'telegram',
                source_turn_id TEXT NOT NULL DEFAULT '',
                created_ts INTEGER NOT NULL,
                expires_ts INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                deleted_ts INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_honcho_memory_owner ON memory_sessions(owner_user_id, status, created_ts);
            CREATE INDEX IF NOT EXISTS idx_honcho_memory_expiry ON memory_sessions(status, expires_ts);
            CREATE INDEX IF NOT EXISTS idx_honcho_memory_chat ON memory_sessions(owner_user_id, chat_id, status);

            CREATE TABLE IF NOT EXISTS preserved_conclusions (
                id TEXT PRIMARY KEY,
                source_session_id TEXT NOT NULL,
                owner_user_id INTEGER,
                owner_key TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                deleted_ts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_honcho_preserved_owner ON preserved_conclusions(owner_user_id, deleted_ts);

            CREATE TABLE IF NOT EXISTS memory_identity_aliases (
                old_owner_key TEXT PRIMARY KEY,
                new_owner_key TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                created_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_exports (
                id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                requested_by_user_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                expires_ts INTEGER NOT NULL,
                downloaded_ts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ready'
            );
            CREATE INDEX IF NOT EXISTS idx_honcho_exports_owner ON memory_exports(owner_user_id, expires_ts);

            CREATE TABLE IF NOT EXISTS memory_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                actor_user_id INTEGER,
                owner_user_id INTEGER,
                resource_id TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_honcho_audit_created ON memory_audit(created_ts);
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_sessions)").fetchall()}
        if "source_kind" not in columns:
            conn.execute("ALTER TABLE memory_sessions ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'telegram'")
        if "source_turn_id" not in columns:
            conn.execute("ALTER TABLE memory_sessions ADD COLUMN source_turn_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def _audit(
    event: str,
    *,
    actor_user_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    resource_id: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_audit(event,actor_user_id,owner_user_id,resource_id,detail_json,created_ts) VALUES(?,?,?,?,?,?)",
            (
                event,
                actor_user_id,
                owner_user_id,
                resource_id,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _safe_resource(prefix: str, canonical: str, *, suffix: str = "") -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    tail = f"_{suffix}" if suffix else ""
    return f"{prefix}_{digest}{tail}"[:512]


def _numeric(value: Any, *, allow_negative: bool = True) -> str:
    raw = str(value or "").strip()
    if allow_negative and raw.startswith("-"):
        digits = raw[1:]
    else:
        digits = raw
    return raw if digits.isdigit() and digits else ""


def resolve_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = _numeric(payload.get("chat_id"))
    telegram_user_id = _numeric(payload.get("telegram_user_id"), allow_negative=False)
    bot_id = _numeric(payload.get("bot_id"), allow_negative=False)
    chat_type = str(payload.get("chat_type") or "").strip().lower()
    if not chat_id or not bot_id or chat_type not in {"private", "group", "supergroup", "channel"}:
        raise ValueError("valid chat_id, bot_id, and chat_type are required")

    linked = telegram_notifications.resolve_linked_nexus_user(
        telegram_user_id=telegram_user_id,
        chat_id=chat_id if chat_type == "private" else None,
    )
    nexus_user_id = int(linked["user_id"]) if linked else None
    if nexus_user_id is not None and telegram_user_id:
        participant_key = f"nexus:{nexus_user_id}:telegram:{telegram_user_id}"
    elif telegram_user_id:
        participant_key = f"telegram:{telegram_user_id}"
    elif nexus_user_id is not None:
        participant_key = f"nexus:{nexus_user_id}"
    else:
        participant_key = f"telegram:chat:{chat_id}:anonymous"

    if chat_type == "private":
        owner_key = participant_key
        partition_key = f"telegram:private:{chat_id}"
        observed_key = owner_key
    else:
        owner_key = f"telegram:group:{chat_id}"
        partition_key = owner_key
        observed_key = owner_key

    aliases = []
    if chat_type == "private" and nexus_user_id is not None and telegram_user_id:
        aliases.append(f"telegram:{telegram_user_id}")
    retrieval_keys = []
    if chat_type == "private" and nexus_user_id is not None:
        retrieval_keys.append(f"nexus:{nexus_user_id}")
    return {
        "owner_user_id": nexus_user_id if chat_type == "private" else None,
        "participant_user_id": nexus_user_id,
        "owner_key": owner_key,
        "participant_key": participant_key,
        "participant_peer_id": _safe_resource("peer", participant_key),
        "observed_key": observed_key,
        "observed_peer_id": _safe_resource("peer", observed_key),
        "partition_key": partition_key,
        "short_term_key": f"{partition_key}:bot:{bot_id}",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "bot_id": bot_id,
        "linked": linked,
        "aliases": aliases,
        "retrieval_keys": retrieval_keys,
        "source_kind": "telegram",
    }


def resolve_ui_identity(*, owner_user_id: int, conversation_id: str, soul_name: str = "") -> Dict[str, Any]:
    try:
        nexus_user_id = int(owner_user_id)
    except Exception as exc:
        raise ValueError("positive owner_user_id is required") from exc
    if nexus_user_id < 1:
        raise ValueError("positive owner_user_id is required")

    chat_id = str(conversation_id or "").strip()
    if not chat_id or len(chat_id) > 256 or any(ord(char) < 32 for char in chat_id):
        raise ValueError("valid conversation_id is required")

    soul = str(soul_name or "nexus").strip().lower() or "nexus"
    if len(soul) > 64 or not all(char.isalnum() or char in {"_", "-"} for char in soul):
        raise ValueError("valid soul_name is required")

    owner_key = f"nexus:{nexus_user_id}"
    linked = telegram_notifications.linked_telegram_identity_for_nexus_user(user_id=nexus_user_id)
    retrieval_keys: list[str] = []
    if linked:
        telegram_user_id = _numeric(linked.get("telegram_user_id"), allow_negative=False)
        if telegram_user_id:
            retrieval_keys.extend(
                [
                    f"nexus:{nexus_user_id}:telegram:{telegram_user_id}",
                    f"telegram:{telegram_user_id}",
                ]
            )

    partition_key = f"nexus:ui:{nexus_user_id}:conversation:{chat_id}"
    return {
        "owner_user_id": nexus_user_id,
        "participant_user_id": nexus_user_id,
        "owner_key": owner_key,
        "participant_key": owner_key,
        "participant_peer_id": _safe_resource("peer", owner_key),
        "observed_key": owner_key,
        "observed_peer_id": _safe_resource("peer", owner_key),
        "partition_key": partition_key,
        "short_term_key": f"{partition_key}:soul:{soul}",
        "chat_id": chat_id,
        "chat_type": "private",
        "bot_id": f"ui:{soul}",
        "linked": linked,
        "aliases": [],
        "retrieval_keys": retrieval_keys,
        "source_kind": "nexus_chat_ui",
        "soul": soul,
    }


def _record_identity_aliases(identity: Dict[str, Any]) -> None:
    owner_user_id = identity.get("owner_user_id")
    new_owner_key = str(identity.get("owner_key") or "")
    aliases = [str(value) for value in identity.get("aliases") or [] if str(value)]
    if owner_user_id is None or not new_owner_key or not aliases:
        return
    now = int(time.time())
    conn = _connect()
    created: list[str] = []
    try:
        for old_owner_key in aliases:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO memory_identity_aliases(old_owner_key,new_owner_key,owner_user_id,created_ts) VALUES(?,?,?,?)",
                (old_owner_key, new_owner_key, int(owner_user_id), now),
            )
            if cursor.rowcount:
                created.append(old_owner_key)
            conn.execute(
                "UPDATE memory_sessions SET owner_user_id=? WHERE owner_user_id IS NULL AND owner_key=?",
                (int(owner_user_id), old_owner_key),
            )
        conn.commit()
    finally:
        conn.close()
    for old_owner_key in created:
        _audit(
            "memory_identity_aliased",
            owner_user_id=int(owner_user_id),
            resource_id=old_owner_key,
            detail={"new_owner_key": new_owner_key},
        )


async def _request(method: str, path: str, *, body: Optional[Dict[str, Any]] = None) -> Any:
    if not enabled():
        raise RuntimeError("Honcho memory is disabled")
    if not configured():
        raise RuntimeError("Honcho memory is not configured")
    headers = {"Authorization": f"Bearer {_token()}"}
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.request(method, f"{_base_url()}{_api_prefix()}{path}", headers=headers, json=body)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _workspace_path(suffix: str = "") -> str:
    base = f"/workspaces/{quote(_workspace_id(), safe='')}"
    return f"{base}{suffix}"


async def _ensure_workspace() -> None:
    await _request("POST", "/workspaces", body={"id": _workspace_id(), "metadata": {"managed_by": "nexus"}})


async def _ensure_peer(peer_id: str, canonical_key: str, kind: str) -> None:
    await _request(
        "POST",
        _workspace_path("/peers"),
        body={"id": peer_id, "metadata": {"canonical_key": canonical_key, "kind": kind}},
    )


def _turn_session_id(identity: Dict[str, Any], source_turn_id: str) -> str:
    source_kind = str(identity.get("source_kind") or "telegram")
    if source_kind == "nexus_chat_ui":
        return _safe_resource("uiturn", f"{identity['short_term_key']}:turn:{source_turn_id}")
    return _safe_resource("tgturn", f"{identity['short_term_key']}:message:{source_turn_id}")


async def _get_context_for_identity(identity: Dict[str, Any], query: str) -> Dict[str, Any]:
    state = status()
    if not state["enabled"]:
        return {**state, "identity": identity, "context": ""}
    _record_identity_aliases(identity)
    target_keys = [
        identity["observed_key"],
        *(identity.get("retrieval_keys") or []),
        *(identity.get("aliases") or []),
    ]
    contexts: list[str] = []
    for target_key in dict.fromkeys(str(value) for value in target_keys if value):
        try:
            response = await _request(
                "POST",
                _workspace_path(f"/peers/{quote(FLEET_OBSERVER_ID, safe='')}/representation"),
                body={
                    "target": _safe_resource("peer", target_key),
                    "search_query": query or None,
                    "search_top_k": 12,
                    "max_conclusions": 20,
                },
            )
            context = str((response or {}).get("representation") or "").strip()
            if context and context not in contexts:
                contexts.append(context)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
    return {**state, "identity": identity, "context": "\n\n".join(contexts)[:16000]}


async def get_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    identity = resolve_identity(payload)
    query = str(payload.get("message") or "").strip()[:12000]
    return await _get_context_for_identity(identity, query)


async def get_ui_context(
    *,
    owner_user_id: int,
    conversation_id: str,
    soul_name: str,
    message: str,
) -> Dict[str, Any]:
    identity = resolve_ui_identity(
        owner_user_id=owner_user_id,
        conversation_id=conversation_id,
        soul_name=soul_name,
    )
    return await _get_context_for_identity(identity, str(message or "").strip()[:12000])


async def _ingest_resolved_turn(
    identity: Dict[str, Any],
    *,
    source_turn_id: str,
    user_text: str,
    assistant_text: str,
) -> Dict[str, Any]:
    state = status()
    if not state["enabled"]:
        return {**state, "stored": False, "identity": identity}
    _record_identity_aliases(identity)
    user_text = str(user_text or "")
    assistant_text = str(assistant_text or "")
    source_turn_id = str(source_turn_id or "").strip()
    if not user_text.strip() or not assistant_text.strip() or not source_turn_id:
        raise ValueError("user_text, assistant_text, and source_turn_id are required")

    now = int(time.time())
    expires_ts = now + _retention_seconds(identity["chat_type"])
    session_id = _turn_session_id(identity, source_turn_id)
    registry_id = hashlib.sha256(f"{_workspace_id()}:{session_id}".encode("utf-8")).hexdigest()
    source_kind = str(identity.get("source_kind") or "telegram")
    telegram_message_id = source_turn_id if source_kind == "telegram" else ""
    metadata = {
        "managed_by": "nexus",
        "source_kind": source_kind,
        "source_turn_id": source_turn_id,
        "retention_class": "group_raw" if identity["chat_type"] != "private" else "private_raw",
        "delete_after_ts": expires_ts,
        "owner_user_id": identity["owner_user_id"],
        "participant_user_id": identity["participant_user_id"],
        "owner_key": identity["owner_key"],
        "participant_key": identity["participant_key"],
        "partition_key": identity["partition_key"],
        "short_term_key": identity["short_term_key"],
        "fleet_key": FLEET_OBSERVER_KEY,
        "chat_id": identity["chat_id"],
        "chat_type": identity["chat_type"],
        "bot_id": identity["bot_id"],
    }
    if telegram_message_id:
        metadata["telegram_message_id"] = telegram_message_id
    if identity.get("soul"):
        metadata["soul"] = str(identity["soul"])

    await _ensure_workspace()
    await _ensure_peer(FLEET_OBSERVER_ID, FLEET_OBSERVER_KEY, "fleet_observer")
    await _ensure_peer(identity["participant_peer_id"], identity["participant_key"], "participant")
    if identity["observed_peer_id"] != identity["participant_peer_id"]:
        await _ensure_peer(identity["observed_peer_id"], identity["observed_key"], "group")

    peers = {
        FLEET_OBSERVER_ID: {"observe_others": True},
        identity["participant_peer_id"]: {"observe_others": False},
    }
    if identity["observed_peer_id"] not in peers:
        peers[identity["observed_peer_id"]] = {"observe_others": False}
    await _request(
        "POST",
        _workspace_path("/sessions"),
        body={"id": session_id, "metadata": metadata, "peers": peers},
    )
    message_metadata = dict(metadata)
    message_peer_id = (
        identity["participant_peer_id"]
        if identity["chat_type"] == "private"
        else identity["observed_peer_id"]
    )
    await _request(
        "POST",
        _workspace_path(f"/sessions/{quote(session_id, safe='')}/messages"),
        body={
            "messages": [
                {"content": user_text[:65535], "peer_id": message_peer_id, "metadata": {**message_metadata, "role": "user"}},
                {"content": assistant_text[:65535], "peer_id": FLEET_OBSERVER_ID, "metadata": {**message_metadata, "role": "assistant"}},
            ]
        },
    )

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO memory_sessions(
                id,honcho_session_id,owner_user_id,owner_key,participant_key,partition_key,
                chat_id,chat_type,bot_id,telegram_message_id,source_kind,source_turn_id,
                created_ts,expires_ts,status,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET expires_ts=excluded.expires_ts, metadata_json=excluded.metadata_json
            """,
            (
                registry_id,
                session_id,
                identity["owner_user_id"],
                identity["owner_key"],
                identity["participant_key"],
                identity["partition_key"],
                identity["chat_id"],
                identity["chat_type"],
                identity["bot_id"],
                telegram_message_id,
                source_kind,
                source_turn_id,
                now,
                expires_ts,
                "active",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {**state, "stored": True, "id": registry_id, "expires_ts": expires_ts, "identity": identity}


async def ingest_turn(payload: Dict[str, Any]) -> Dict[str, Any]:
    telegram_message_id = _numeric(payload.get("telegram_message_id"), allow_negative=False)
    if not telegram_message_id:
        raise ValueError("numeric telegram_message_id is required")
    return await _ingest_resolved_turn(
        resolve_identity(payload),
        source_turn_id=telegram_message_id,
        user_text=str(payload.get("user_text") or ""),
        assistant_text=str(payload.get("assistant_text") or ""),
    )


async def ingest_ui_turn(
    *,
    owner_user_id: int,
    conversation_id: str,
    soul_name: str,
    turn_id: str,
    user_text: str,
    assistant_text: str,
) -> Dict[str, Any]:
    return await _ingest_resolved_turn(
        resolve_ui_identity(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            soul_name=soul_name,
        ),
        source_turn_id=str(turn_id or "").strip(),
        user_text=user_text,
        assistant_text=assistant_text,
    )


def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    if "metadata_json" in item:
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
    return item


def list_sessions_for_user(owner_user_id: int, *, include_deleted: bool = False) -> list[Dict[str, Any]]:
    init()
    conn = _connect()
    try:
        sql = "SELECT * FROM memory_sessions WHERE owner_user_id=?"
        args: list[Any] = [int(owner_user_id)]
        if not include_deleted:
            sql += " AND status='active'"
        sql += " ORDER BY created_ts DESC"
        return [_row_dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


async def _list_messages(session_id: str) -> list[Dict[str, Any]]:
    payload = await _request("POST", _workspace_path(f"/sessions/{quote(session_id, safe='')}/messages/list"), body={})
    return list((payload or {}).get("items") or [])


async def _list_conclusions(filters: Dict[str, Any]) -> list[Dict[str, Any]]:
    payload = await _request("POST", _workspace_path("/conclusions/list"), body={"filters": filters})
    return list((payload or {}).get("items") or [])


async def _delete_conclusions(ids: Iterable[str]) -> int:
    deleted = 0
    for conclusion_id in ids:
        if not conclusion_id:
            continue
        await _request("DELETE", _workspace_path(f"/conclusions/{quote(str(conclusion_id), safe='')}"))
        deleted += 1
    return deleted


async def delete_session_for_user(registry_id: str, *, owner_user_id: int, actor_user_id: int) -> Dict[str, Any]:
    init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM memory_sessions WHERE id=? AND owner_user_id=?",
            (registry_id, int(owner_user_id)),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError("memory session not found")
    item = _row_dict(row)
    if item["status"] == "active":
        await _request("DELETE", _workspace_path(f"/sessions/{quote(item['honcho_session_id'], safe='')}"))
    preserved = _preserved_ids(registry_id)
    await _delete_conclusions(preserved)
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute("UPDATE memory_sessions SET status='deleted', deleted_ts=? WHERE id=?", (now, registry_id))
        conn.execute("UPDATE preserved_conclusions SET deleted_ts=? WHERE source_session_id=?", (now, registry_id))
        conn.commit()
    finally:
        conn.close()
    _expire_exports_for_user(owner_user_id, status="deleted")
    _audit("memory_session_deleted", actor_user_id=actor_user_id, owner_user_id=owner_user_id, resource_id=registry_id)
    return {"ok": True, "id": registry_id}


def _preserved_ids(registry_id: str) -> list[str]:
    conn = _connect()
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM preserved_conclusions WHERE source_session_id=? AND deleted_ts=0",
                (registry_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


async def delete_all_for_user(*, owner_user_id: int, actor_user_id: int) -> Dict[str, Any]:
    sessions = list_sessions_for_user(owner_user_id, include_deleted=True)
    deleted_sessions = 0
    owner_keys: set[str] = set()
    for item in sessions:
        owner_keys.add(str(item["owner_key"]))
        if item["status"] == "deleted":
            continue
        await delete_session_for_user(str(item["id"]), owner_user_id=owner_user_id, actor_user_id=actor_user_id)
        deleted_sessions += 1
    deleted_conclusions = 0
    for owner_key in owner_keys:
        observed_id = _safe_resource("peer", owner_key)
        conclusions = await _list_conclusions({"observed": observed_id})
        deleted_conclusions += await _delete_conclusions(str(item.get("id") or "") for item in conclusions)
    expired_exports = _expire_exports_for_user(owner_user_id, status="deleted")
    _audit(
        "memory_owner_deleted",
        actor_user_id=actor_user_id,
        owner_user_id=owner_user_id,
        detail={"sessions": deleted_sessions, "conclusions": deleted_conclusions, "exports": expired_exports},
    )
    return {
        "ok": True,
        "sessions_deleted": deleted_sessions,
        "conclusions_deleted": deleted_conclusions,
        "exports_deleted": expired_exports,
    }


def _expire_exports_for_user(owner_user_id: int, *, status: str) -> int:
    conn = _connect()
    removed = 0
    try:
        rows = conn.execute(
            "SELECT id,path FROM memory_exports WHERE owner_user_id=? AND status='ready'",
            (int(owner_user_id),),
        ).fetchall()
        for row in rows:
            path = Path(str(row["path"]))
            try:
                if path.is_file() and path.parent.resolve() == _export_dir().resolve():
                    path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove memory export %s during deletion (%s)", row["id"], exc)
                continue
            conn.execute("UPDATE memory_exports SET status=? WHERE id=?", (status, row["id"]))
            removed += 1
        conn.commit()
    finally:
        conn.close()
    return removed


async def create_export(*, owner_user_id: int, requested_by_user_id: int) -> Dict[str, Any]:
    init()
    sessions = list_sessions_for_user(owner_user_id, include_deleted=False)
    exported_sessions: list[Dict[str, Any]] = []
    owner_keys: set[str] = set()
    for item in sessions:
        owner_keys.add(str(item["owner_key"]))
        messages: list[Dict[str, Any]] = []
        try:
            messages = await _list_messages(str(item["honcho_session_id"]))
        except Exception as exc:
            logger.warning("Honcho export skipped session %s (%s: %s)", item["id"], type(exc).__name__, exc)
        exported_sessions.append({"registry": item, "messages": messages})
    conclusions: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for owner_key in owner_keys:
        for item in await _list_conclusions({"observed": _safe_resource("peer", owner_key)}):
            conclusion_id = str(item.get("id") or "")
            if conclusion_id and conclusion_id not in seen:
                seen.add(conclusion_id)
                conclusions.append(item)

    now = int(time.time())
    export_id = uuid.uuid4().hex
    payload = {
        "schema": "nexus-honcho-memory-export-v1",
        "owner_nexus_user_id": int(owner_user_id),
        "created_ts": now,
        "sessions": exported_sessions,
        "conclusions": conclusions,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    checksum = hashlib.sha256(raw).hexdigest()
    directory = _export_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"memory-export-{export_id}.json"
    temp = directory / f".{export_id}.tmp"
    temp.write_bytes(raw)
    os.chmod(temp, 0o600)
    temp.replace(path)
    os.chmod(path, 0o600)
    expires_ts = now + _export_retention_seconds()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_exports(id,owner_user_id,requested_by_user_id,path,checksum_sha256,created_ts,expires_ts,status) VALUES(?,?,?,?,?,?,?,'ready')",
            (export_id, int(owner_user_id), int(requested_by_user_id), str(path), checksum, now, expires_ts),
        )
        conn.commit()
    finally:
        conn.close()
    _audit(
        "memory_export_created",
        actor_user_id=requested_by_user_id,
        owner_user_id=owner_user_id,
        resource_id=export_id,
        detail={"checksum_sha256": checksum, "expires_ts": expires_ts},
    )
    return {"id": export_id, "checksum_sha256": checksum, "created_ts": now, "expires_ts": expires_ts, "status": "ready"}


def list_exports_for_user(owner_user_id: int) -> list[Dict[str, Any]]:
    init()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id,checksum_sha256,created_ts,expires_ts,downloaded_ts,status FROM memory_exports WHERE owner_user_id=? ORDER BY created_ts DESC",
            (int(owner_user_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def export_for_download(export_id: str, *, owner_user_id: int) -> Dict[str, Any]:
    init()
    now = int(time.time())
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM memory_exports WHERE id=? AND owner_user_id=? AND status='ready' AND expires_ts>?",
            (export_id, int(owner_user_id), now),
        ).fetchone()
        if row is None:
            raise KeyError("memory export not found")
        path = Path(str(row["path"]))
        if not path.is_file() or path.parent.resolve() != _export_dir().resolve():
            raise KeyError("memory export file not found")
        conn.execute("UPDATE memory_exports SET downloaded_ts=? WHERE id=?", (now, export_id))
        conn.commit()
        result = dict(row)
        result["path"] = str(path)
    finally:
        conn.close()
    _audit("memory_export_downloaded", actor_user_id=owner_user_id, owner_user_id=owner_user_id, resource_id=export_id)
    return result


async def _preserve_conclusions(item: Dict[str, Any]) -> int:
    conclusions = await _list_conclusions({"session_id": item["honcho_session_id"]})
    if not conclusions:
        return 0
    body = {"conclusions": []}
    for conclusion in conclusions:
        content = str(conclusion.get("content") or "").strip()
        observer = str(conclusion.get("observer_id") or conclusion.get("observer") or "").strip()
        observed = str(conclusion.get("observed_id") or conclusion.get("observed") or "").strip()
        if content and observer and observed:
            body["conclusions"].append({"content": content, "observer_id": observer, "observed_id": observed})
    if not body["conclusions"]:
        return 0
    created = await _request("POST", _workspace_path("/conclusions"), body=body)
    now = int(time.time())
    conn = _connect()
    try:
        for conclusion in created or []:
            conclusion_id = str(conclusion.get("id") or "")
            if conclusion_id:
                conn.execute(
                    "INSERT OR IGNORE INTO preserved_conclusions(id,source_session_id,owner_user_id,owner_key,created_ts) VALUES(?,?,?,?,?)",
                    (conclusion_id, item["id"], item["owner_user_id"], item["owner_key"], now),
                )
        conn.commit()
    finally:
        conn.close()
    return len(created or [])


async def run_maintenance_once() -> Dict[str, Any]:
    init()
    async with _MAINTENANCE_LOCK:
        now = int(time.time())
        conn = _connect()
        try:
            expired = [
                _row_dict(row)
                for row in conn.execute(
                    "SELECT * FROM memory_sessions WHERE status='active' AND expires_ts<=? ORDER BY expires_ts ASC LIMIT 500",
                    (now,),
                ).fetchall()
            ]
        finally:
            conn.close()
        deleted = 0
        preserved = 0
        for item in expired:
            try:
                preserved += await _preserve_conclusions(item)
                await _request("DELETE", _workspace_path(f"/sessions/{quote(item['honcho_session_id'], safe='')}"))
                conn = _connect()
                try:
                    conn.execute("UPDATE memory_sessions SET status='expired', deleted_ts=? WHERE id=?", (now, item["id"]))
                    conn.commit()
                finally:
                    conn.close()
                deleted += 1
            except Exception as exc:
                logger.warning("Honcho retention failed for %s (%s: %s)", item["id"], type(exc).__name__, exc)

        # Reapply deletion tombstones after a database restore. Honcho returns
        # 404 for already-absent sessions, which _request treats as success.
        conn = _connect()
        try:
            tombstones = [
                str(row[0])
                for row in conn.execute(
                    "SELECT honcho_session_id FROM memory_sessions WHERE status IN ('deleted','expired') ORDER BY deleted_ts DESC LIMIT 500"
                ).fetchall()
            ]
        finally:
            conn.close()
        tombstones_reapplied = 0
        for session_id in tombstones:
            try:
                await _request("DELETE", _workspace_path(f"/sessions/{quote(session_id, safe='')}"))
                tombstones_reapplied += 1
            except Exception as exc:
                logger.warning("Honcho tombstone reapply failed for %s (%s: %s)", session_id, type(exc).__name__, exc)

        conn = _connect()
        try:
            export_rows = conn.execute(
                "SELECT id,path FROM memory_exports WHERE status='ready' AND expires_ts<=?",
                (now,),
            ).fetchall()
            expired_exports = 0
            for row in export_rows:
                path = Path(str(row["path"]))
                try:
                    if path.is_file() and path.parent.resolve() == _export_dir().resolve():
                        path.unlink()
                except Exception as exc:
                    logger.warning("Failed to remove expired memory export %s (%s)", row["id"], exc)
                    continue
                conn.execute("UPDATE memory_exports SET status='expired' WHERE id=?", (row["id"],))
                expired_exports += 1
            conn.execute("DELETE FROM memory_audit WHERE created_ts<?", (now - _audit_retention_seconds(),))
            conn.commit()
        finally:
            conn.close()
        return {
            "sessions_expired": deleted,
            "conclusions_preserved": preserved,
            "exports_expired": expired_exports,
            "tombstones_reapplied": tombstones_reapplied,
        }


async def _maintenance_loop() -> None:
    while not _MAINTENANCE_STOP.is_set():
        if enabled() and configured():
            try:
                await run_maintenance_once()
            except Exception as exc:
                logger.warning("Honcho maintenance unavailable (%s: %s)", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(_MAINTENANCE_STOP.wait(), timeout=_maintenance_interval())
        except asyncio.TimeoutError:
            continue


async def start_maintenance() -> None:
    global _MAINTENANCE_TASK
    init()
    if _MAINTENANCE_TASK is not None and not _MAINTENANCE_TASK.done():
        return
    _MAINTENANCE_STOP.clear()
    _MAINTENANCE_TASK = asyncio.create_task(_maintenance_loop(), name="honcho-memory-maintenance")


async def stop_maintenance() -> None:
    global _MAINTENANCE_TASK
    _MAINTENANCE_STOP.set()
    task = _MAINTENANCE_TASK
    _MAINTENANCE_TASK = None
    if task is not None:
        await task
