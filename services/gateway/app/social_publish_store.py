from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional


def _now() -> int:
    return int(time.time())


def _db(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except Exception:
        return fallback
    return parsed


def init_schema(db_path: str) -> None:
    conn = _db(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_oauth_states (
              state TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              redirect_after TEXT NOT NULL,
              context_json TEXT NOT NULL DEFAULT '{}',
              created_ts INTEGER NOT NULL,
              expires_ts INTEGER NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS social_connected_accounts (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              external_account_id TEXT NOT NULL,
              display_name TEXT NOT NULL,
              account_type TEXT NOT NULL,
              scopes_json TEXT NOT NULL DEFAULT '[]',
              access_token_enc TEXT NOT NULL,
              refresh_token_enc TEXT,
              token_type TEXT NOT NULL DEFAULT 'Bearer',
              access_expires_ts INTEGER,
              refresh_expires_ts INTEGER,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_ts INTEGER NOT NULL,
              updated_ts INTEGER NOT NULL,
              revoked_ts INTEGER,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              UNIQUE(user_id, provider, external_account_id)
            );

            CREATE TABLE IF NOT EXISTS social_media_assets (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              path TEXT NOT NULL,
              filename TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_ts INTEGER NOT NULL,
              expires_ts INTEGER NOT NULL,
              deleted_ts INTEGER,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS social_publications (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              media_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              status TEXT NOT NULL,
              request_json TEXT NOT NULL DEFAULT '{}',
              response_json TEXT NOT NULL DEFAULT '{}',
              remote_id TEXT,
              remote_context_json TEXT NOT NULL DEFAULT '{}',
              session_secret_enc TEXT,
              error_json TEXT NOT NULL DEFAULT '{}',
              consent_ts INTEGER,
              created_ts INTEGER NOT NULL,
              updated_ts INTEGER NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY (account_id) REFERENCES social_connected_accounts(id) ON DELETE CASCADE,
              FOREIGN KEY (media_id) REFERENCES social_media_assets(id) ON DELETE CASCADE,
              UNIQUE(user_id, provider, account_id, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_social_oauth_user ON social_oauth_states(user_id, expires_ts);
            CREATE INDEX IF NOT EXISTS idx_social_accounts_user ON social_connected_accounts(user_id, provider, revoked_ts);
            CREATE INDEX IF NOT EXISTS idx_social_media_user ON social_media_assets(user_id, created_ts);
            CREATE INDEX IF NOT EXISTS idx_social_publications_user ON social_publications(user_id, updated_ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def create_oauth_state(
    db_path: str,
    *,
    user_id: int,
    provider: str,
    redirect_after: str,
    ttl_sec: int,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    init_schema(db_path)
    state = secrets.token_urlsafe(32)
    now = _now()
    conn = _db(db_path)
    try:
        conn.execute("DELETE FROM social_oauth_states WHERE expires_ts < ?", (now,))
        conn.execute(
            "INSERT INTO social_oauth_states(state,user_id,provider,redirect_after,context_json,created_ts,expires_ts) VALUES(?,?,?,?,?,?,?)",
            (state, int(user_id), provider, redirect_after, _dump(context or {}), now, now + max(60, int(ttl_sec))),
        )
        conn.commit()
    finally:
        conn.close()
    return state


def consume_oauth_state(db_path: str, *, state: str, provider: str) -> Optional[Dict[str, Any]]:
    init_schema(db_path)
    now = _now()
    normalized_state = (state or "").strip()
    normalized_provider = (provider or "").strip()
    conn = _db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM social_oauth_states WHERE state=? AND provider=? AND expires_ts>=?",
            (normalized_state, normalized_provider, now),
        ).fetchone()
        if row is not None:
            conn.execute(
                "DELETE FROM social_oauth_states WHERE state=? AND provider=?",
                (normalized_state, normalized_provider),
            )
        conn.execute("DELETE FROM social_oauth_states WHERE expires_ts < ?", (now,))
        conn.commit()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "state": row["state"],
        "user_id": int(row["user_id"]),
        "provider": row["provider"],
        "redirect_after": row["redirect_after"],
        "context": _load(row["context_json"], {}),
        "created_ts": int(row["created_ts"]),
        "expires_ts": int(row["expires_ts"]),
    }


def upsert_account(
    db_path: str,
    *,
    user_id: int,
    provider: str,
    external_account_id: str,
    display_name: str,
    account_type: str,
    scopes: List[str],
    access_token_enc: str,
    refresh_token_enc: Optional[str],
    token_type: str,
    access_expires_ts: Optional[int],
    refresh_expires_ts: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    init_schema(db_path)
    now = _now()
    conn = _db(db_path)
    try:
        existing = conn.execute(
            "SELECT id,created_ts FROM social_connected_accounts WHERE user_id=? AND provider=? AND external_account_id=?",
            (int(user_id), provider, external_account_id),
        ).fetchone()
        account_id = str(existing["id"]) if existing else secrets.token_urlsafe(12).replace("-", "_")
        created_ts = int(existing["created_ts"]) if existing else now
        conn.execute(
            """
            INSERT INTO social_connected_accounts(
              id,user_id,provider,external_account_id,display_name,account_type,scopes_json,
              access_token_enc,refresh_token_enc,token_type,access_expires_ts,refresh_expires_ts,
              metadata_json,created_ts,updated_ts,revoked_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(user_id,provider,external_account_id) DO UPDATE SET
              display_name=excluded.display_name,
              account_type=excluded.account_type,
              scopes_json=excluded.scopes_json,
              access_token_enc=excluded.access_token_enc,
              refresh_token_enc=COALESCE(excluded.refresh_token_enc,social_connected_accounts.refresh_token_enc),
              token_type=excluded.token_type,
              access_expires_ts=excluded.access_expires_ts,
              refresh_expires_ts=COALESCE(excluded.refresh_expires_ts,social_connected_accounts.refresh_expires_ts),
              metadata_json=excluded.metadata_json,
              updated_ts=excluded.updated_ts,
              revoked_ts=NULL
            """,
            (
                account_id,
                int(user_id),
                provider,
                external_account_id,
                display_name,
                account_type,
                _dump(scopes),
                access_token_enc,
                refresh_token_enc,
                token_type or "Bearer",
                access_expires_ts,
                refresh_expires_ts,
                _dump(metadata or {}),
                created_ts,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    account = get_account(db_path, user_id=user_id, account_id=account_id, include_secrets=False)
    if account is None:
        raise RuntimeError("failed to save connected account")
    return account


def _account_from_row(row: sqlite3.Row, *, include_secrets: bool) -> Dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "user_id": int(row["user_id"]),
        "provider": str(row["provider"]),
        "external_account_id": str(row["external_account_id"]),
        "display_name": str(row["display_name"] or ""),
        "account_type": str(row["account_type"] or ""),
        "scopes": _load(row["scopes_json"], []),
        "token_type": str(row["token_type"] or "Bearer"),
        "access_expires_ts": int(row["access_expires_ts"]) if row["access_expires_ts"] is not None else None,
        "refresh_expires_ts": int(row["refresh_expires_ts"]) if row["refresh_expires_ts"] is not None else None,
        "metadata": _load(row["metadata_json"], {}),
        "created_ts": int(row["created_ts"]),
        "updated_ts": int(row["updated_ts"]),
        "revoked_ts": int(row["revoked_ts"]) if row["revoked_ts"] is not None else None,
    }
    if include_secrets:
        result["access_token_enc"] = str(row["access_token_enc"] or "")
        result["refresh_token_enc"] = str(row["refresh_token_enc"] or "") or None
    return result


def list_accounts(db_path: str, *, user_id: int, include_revoked: bool = False) -> List[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        query = "SELECT * FROM social_connected_accounts WHERE user_id=?"
        args: List[Any] = [int(user_id)]
        if not include_revoked:
            query += " AND revoked_ts IS NULL"
        query += " ORDER BY provider,display_name"
        rows = conn.execute(query, args).fetchall()
    finally:
        conn.close()
    return [_account_from_row(row, include_secrets=False) for row in rows]


def get_account(db_path: str, *, user_id: int, account_id: str, include_secrets: bool = True) -> Optional[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM social_connected_accounts WHERE id=? AND user_id=?",
            (account_id, int(user_id)),
        ).fetchone()
    finally:
        conn.close()
    return _account_from_row(row, include_secrets=include_secrets) if row else None


def update_account_tokens(
    db_path: str,
    *,
    user_id: int,
    account_id: str,
    access_token_enc: str,
    refresh_token_enc: Optional[str],
    access_expires_ts: Optional[int],
    refresh_expires_ts: Optional[int],
    scopes: Optional[List[str]] = None,
) -> None:
    init_schema(db_path)
    now = _now()
    conn = _db(db_path)
    try:
        current = conn.execute(
            "SELECT scopes_json FROM social_connected_accounts WHERE id=? AND user_id=?",
            (account_id, int(user_id)),
        ).fetchone()
        if current is None:
            raise FileNotFoundError("connected account not found")
        conn.execute(
            """
            UPDATE social_connected_accounts SET access_token_enc=?,
              refresh_token_enc=COALESCE(?,refresh_token_enc), access_expires_ts=?,
              refresh_expires_ts=COALESCE(?,refresh_expires_ts), scopes_json=?, updated_ts=?, revoked_ts=NULL
            WHERE id=? AND user_id=?
            """,
            (
                access_token_enc,
                refresh_token_enc,
                access_expires_ts,
                refresh_expires_ts,
                _dump(scopes if scopes is not None else _load(current["scopes_json"], [])),
                now,
                account_id,
                int(user_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def revoke_account(db_path: str, *, user_id: int, account_id: str) -> bool:
    init_schema(db_path)
    now = _now()
    conn = _db(db_path)
    try:
        cur = conn.execute(
            "UPDATE social_connected_accounts SET revoked_ts=?,updated_ts=? WHERE id=? AND user_id=? AND revoked_ts IS NULL",
            (now, now, account_id, int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def create_media_asset(
    db_path: str,
    *,
    user_id: int,
    path: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str,
    metadata: Optional[Dict[str, Any]],
    ttl_sec: int,
) -> Dict[str, Any]:
    init_schema(db_path)
    now = _now()
    media_id = secrets.token_urlsafe(12).replace("-", "_")
    conn = _db(db_path)
    try:
        conn.execute(
            "INSERT INTO social_media_assets(id,user_id,path,filename,mime_type,size_bytes,sha256,metadata_json,created_ts,expires_ts,deleted_ts) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (media_id, int(user_id), path, filename, mime_type, int(size_bytes), sha256, _dump(metadata or {}), now, now + max(300, int(ttl_sec))),
        )
        conn.commit()
    finally:
        conn.close()
    result = get_media_asset(db_path, user_id=user_id, media_id=media_id)
    if result is None:
        raise RuntimeError("failed to save media asset")
    return result


def _media_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "user_id": int(row["user_id"]),
        "path": str(row["path"]),
        "filename": str(row["filename"]),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]),
        "metadata": _load(row["metadata_json"], {}),
        "created_ts": int(row["created_ts"]),
        "expires_ts": int(row["expires_ts"]),
        "deleted_ts": int(row["deleted_ts"]) if row["deleted_ts"] is not None else None,
    }


def get_media_asset(db_path: str, *, user_id: int, media_id: str) -> Optional[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM social_media_assets WHERE id=? AND user_id=? AND deleted_ts IS NULL",
            (media_id, int(user_id)),
        ).fetchone()
    finally:
        conn.close()
    return _media_from_row(row) if row else None


def get_public_media_asset(db_path: str, *, media_id: str) -> Optional[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM social_media_assets WHERE id=? AND deleted_ts IS NULL",
            (media_id,),
        ).fetchone()
    finally:
        conn.close()
    return _media_from_row(row) if row else None


def list_media_assets(db_path: str, *, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    init_schema(db_path)
    now = _now()
    conn = _db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM social_media_assets WHERE user_id=? AND deleted_ts IS NULL AND expires_ts>=? ORDER BY created_ts DESC LIMIT ?",
            (int(user_id), now, max(1, min(200, int(limit)))),
        ).fetchall()
    finally:
        conn.close()
    return [_media_from_row(row) for row in rows]


def create_or_get_publication(
    db_path: str,
    *,
    user_id: int,
    provider: str,
    account_id: str,
    media_id: str,
    idempotency_key: str,
    request_payload: Dict[str, Any],
    consent_ts: Optional[int],
) -> Dict[str, Any]:
    init_schema(db_path)
    now = _now()
    publication_id = secrets.token_urlsafe(12).replace("-", "_")
    conn = _db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO social_publications(
              id,user_id,provider,account_id,media_id,idempotency_key,status,request_json,response_json,
              remote_context_json,error_json,consent_ts,created_ts,updated_ts
            ) VALUES(?,?,?,?,?,?,?,?,'{}','{}','{}',?,?,?)
            ON CONFLICT(user_id,provider,account_id,idempotency_key) DO NOTHING
            """,
            (
                publication_id,
                int(user_id),
                provider,
                account_id,
                media_id,
                idempotency_key,
                "READY",
                _dump(request_payload),
                consent_ts,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM social_publications WHERE user_id=? AND provider=? AND account_id=? AND idempotency_key=?",
            (int(user_id), provider, account_id, idempotency_key),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    result = get_publication(db_path, user_id=user_id, publication_id=str(row["id"])) if row else None
    if result is None:
        raise RuntimeError("failed to create publication")
    return result


def _publication_from_row(row: sqlite3.Row, *, include_secret: bool) -> Dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "user_id": int(row["user_id"]),
        "provider": str(row["provider"]),
        "account_id": str(row["account_id"]),
        "media_id": str(row["media_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "status": str(row["status"]),
        "request": _load(row["request_json"], {}),
        "response": _load(row["response_json"], {}),
        "remote_id": str(row["remote_id"] or "") or None,
        "remote_context": _load(row["remote_context_json"], {}),
        "error": _load(row["error_json"], {}),
        "consent_ts": int(row["consent_ts"]) if row["consent_ts"] is not None else None,
        "created_ts": int(row["created_ts"]),
        "updated_ts": int(row["updated_ts"]),
    }
    if include_secret:
        result["session_secret_enc"] = str(row["session_secret_enc"] or "") or None
    return result


def get_publication(db_path: str, *, user_id: int, publication_id: str, include_secret: bool = True) -> Optional[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM social_publications WHERE id=? AND user_id=?",
            (publication_id, int(user_id)),
        ).fetchone()
    finally:
        conn.close()
    return _publication_from_row(row, include_secret=include_secret) if row else None


def list_publications(db_path: str, *, user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    init_schema(db_path)
    conn = _db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM social_publications WHERE user_id=? ORDER BY updated_ts DESC LIMIT ?",
            (int(user_id), max(1, min(500, int(limit)))),
        ).fetchall()
    finally:
        conn.close()
    return [_publication_from_row(row, include_secret=False) for row in rows]


def update_publication(
    db_path: str,
    *,
    user_id: int,
    publication_id: str,
    status: Optional[str] = None,
    response: Optional[Dict[str, Any]] = None,
    remote_id: Optional[str] = None,
    remote_context: Optional[Dict[str, Any]] = None,
    session_secret_enc: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    init_schema(db_path)
    current = get_publication(db_path, user_id=user_id, publication_id=publication_id, include_secret=True)
    if current is None:
        raise FileNotFoundError("publication not found")
    conn = _db(db_path)
    try:
        conn.execute(
            """
            UPDATE social_publications SET status=?,response_json=?,remote_id=?,remote_context_json=?,
              session_secret_enc=?,error_json=?,updated_ts=? WHERE id=? AND user_id=?
            """,
            (
                status if status is not None else current["status"],
                _dump(response if response is not None else current["response"]),
                remote_id if remote_id is not None else current["remote_id"],
                _dump(remote_context if remote_context is not None else current["remote_context"]),
                session_secret_enc if session_secret_enc is not None else current.get("session_secret_enc"),
                _dump(error if error is not None else current["error"]),
                _now(),
                publication_id,
                int(user_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    result = get_publication(db_path, user_id=user_id, publication_id=publication_id, include_secret=False)
    if result is None:
        raise RuntimeError("failed to update publication")
    return result
