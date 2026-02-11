from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


def _now() -> int:
    return int(time.time())


def _db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: str) -> None:
    conn = _db(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0,
                admin INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
          token TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          created_ts INTEGER NOT NULL,
          expires_ts INTEGER NOT NULL,
          FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
          user_id INTEGER PRIMARY KEY,
          settings_json TEXT NOT NULL,
          updated_ts INTEGER NOT NULL,
          FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_conversations (
          id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          created_ts INTEGER NOT NULL,
          updated_ts INTEGER NOT NULL,
          summary TEXT NOT NULL,
          messages_json TEXT NOT NULL,
          FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_conversations_user ON user_conversations(user_id, updated_ts);")
    conn.commit()
    conn.close()
    # Ensure legacy DBs have the 'admin' column
    try:
        conn = _db(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users);").fetchall()]
        if "admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN admin INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        conn.close()
    except Exception:
        # Best-effort: if PRAGMA/ALTER fails (old sqlite?), ignore and continue.
        try:
            conn.close()
        except Exception:
            pass


def _hash_password(password: str, salt: bytes) -> str:
    import hashlib

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return digest.hex()


def _new_salt() -> bytes:
    return os.urandom(16)


@dataclass
class User:
    id: int
    username: str
    disabled: bool
    admin: bool = False


@dataclass
class Session:
    token: str
    user_id: int
    expires_ts: int


def _default_settings() -> Dict[str, Any]:
    return {
        "tokens": {},
        "tool_ui": {},
        "auto_detect": {
            "images": True,
            "music": True,
            "video": True,
        },
        "chat": {
            "history": True,
            "model_preference": "default",
        },
        "profile": {"system_prompt": "", "tone": ""},
    }


def create_user(db_path: str, *, username: str, password: str) -> User:
    uname = (username or "").strip().lower()
    if not uname:
        raise ValueError("username required")
    if not password:
        raise ValueError("password required")

    salt = _new_salt()
    phash = _hash_password(password, salt)
    now = _now()
    conn = _db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,password_salt,created_ts,updated_ts,disabled,admin) VALUES(?,?,?,?,?,?,0,0)",
            (uname, phash, salt.hex(), now, now),
        )
        user_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO user_settings(user_id,settings_json,updated_ts) VALUES(?,?,?)",
            (user_id, json.dumps(_default_settings(), ensure_ascii=False), now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError("username already exists") from e
    finally:
        conn.close()
    return User(id=user_id, username=uname, disabled=False)


def create_user_with_admin(db_path: str, *, username: str, password: str, admin: bool = False) -> User:
    # Backwards-compatible wrapper that allows creating admin users.
    uname = (username or "").strip().lower()
    if not uname:
        raise ValueError("username required")
    if not password:
        raise ValueError("password required")

    salt = _new_salt()
    phash = _hash_password(password, salt)
    now = _now()
    conn = _db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,password_salt,created_ts,updated_ts,disabled,admin) VALUES(?,?,?,?,?,?,?)",
            (uname, phash, salt.hex(), now, now, 0, 1 if admin else 0),
        )
        user_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO user_settings(user_id,settings_json,updated_ts) VALUES(?,?,?)",
            (user_id, json.dumps(_default_settings(), ensure_ascii=False), now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError("username already exists") from e
    finally:
        conn.close()
    return User(id=user_id, username=uname, disabled=False, admin=bool(admin))


def set_password(db_path: str, *, username: str, password: str) -> None:
    uname = (username or "").strip().lower()
    if not uname:
        raise ValueError("username required")
    if not password:
        raise ValueError("password required")

    salt = _new_salt()
    phash = _hash_password(password, salt)
    now = _now()
    conn = _db(db_path)
    cur = conn.execute("UPDATE users SET password_hash=?, password_salt=?, updated_ts=? WHERE username=?", (phash, salt.hex(), now, uname))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("user not found")


def set_admin(db_path: str, *, username: str, admin: bool = True) -> None:
    uname = (username or "").strip().lower()
    conn = _db(db_path)
    cur = conn.execute("UPDATE users SET admin=?, updated_ts=? WHERE username=?", (1 if admin else 0, _now(), uname))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("user not found")


def disable_user(db_path: str, *, username: str, disabled: bool = True) -> None:
    uname = (username or "").strip().lower()
    conn = _db(db_path)
    cur = conn.execute("UPDATE users SET disabled=?, updated_ts=? WHERE username=?", (1 if disabled else 0, _now(), uname))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("user not found")


def delete_user(db_path: str, *, username: str) -> None:
    uname = (username or "").strip().lower()
    conn = _db(db_path)
    cur = conn.execute("DELETE FROM users WHERE username=?", (uname,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("user not found")


def list_users(db_path: str) -> List[User]:
    conn = _db(db_path)
    rows = conn.execute("SELECT id, username, disabled, admin FROM users ORDER BY username ASC").fetchall()
    conn.close()
    return [User(id=int(r[0]), username=str(r[1]), disabled=bool(r[2]), admin=bool(r[3])) for r in rows]


def authenticate(db_path: str, *, username: str, password: str) -> Optional[User]:
    uname = (username or "").strip().lower()
    if not uname or not password:
        return None
    conn = _db(db_path)
    row = conn.execute(
        "SELECT id, username, password_hash, password_salt, disabled, admin FROM users WHERE username=?",
        (uname,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    user_id, uname, phash, psalt, disabled, admin = row
    if disabled:
        return None
    try:
        salt = bytes.fromhex(psalt)
    except Exception:
        return None
    if _hash_password(password, salt) != phash:
        return None
    return User(id=int(user_id), username=str(uname), disabled=False, admin=bool(admin))


def create_session(db_path: str, *, user_id: int, ttl_sec: int) -> Session:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + max(60, int(ttl_sec))
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO user_sessions(token,user_id,created_ts,expires_ts) VALUES(?,?,?,?)",
        (token, int(user_id), now, expires),
    )
    conn.commit()
    conn.close()
    return Session(token=token, user_id=int(user_id), expires_ts=expires)


def _purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM user_sessions WHERE expires_ts < ?", (_now(),))


def get_user_by_session(db_path: str, *, token: str) -> Optional[User]:
    if not token:
        return None
    conn = _db(db_path)
    _purge_expired_sessions(conn)
    row = conn.execute(
        """
        SELECT u.id, u.username, u.disabled, u.admin
        FROM user_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_ts >= ?
        """,
        (token, _now()),
    ).fetchone()
    conn.commit()
    conn.close()
    if not row:
        return None
    user_id, uname, disabled, admin = row
    if disabled:
        return None
    return User(id=int(user_id), username=str(uname), disabled=False, admin=bool(admin))


def delete_session(db_path: str, *, token: str) -> None:
    if not token:
        return
    conn = _db(db_path)
    conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def get_settings(db_path: str, *, user_id: int) -> Dict[str, Any]:
    conn = _db(db_path)
    row = conn.execute("SELECT settings_json FROM user_settings WHERE user_id=?", (int(user_id),)).fetchone()
    conn.close()
    if not row:
        return _default_settings()
    try:
        payload = json.loads(row[0])
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return _default_settings()


def set_settings(db_path: str, *, user_id: int, settings: Dict[str, Any]) -> None:
    if not isinstance(settings, dict):
        raise ValueError("settings must be object")
    now = _now()
    payload = json.dumps(settings, ensure_ascii=False)
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO user_settings(user_id,settings_json,updated_ts) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json, updated_ts=excluded.updated_ts",
        (int(user_id), payload, now),
    )
    conn.commit()
    conn.close()


def new_conversation_id() -> str:
    return secrets.token_urlsafe(18).replace("-", "_")


def list_conversations(db_path: str, *, user_id: int) -> List[Dict[str, Any]]:
    conn = _db(db_path)
    rows = conn.execute(
        "SELECT id, created_ts, updated_ts, summary FROM user_conversations WHERE user_id=? ORDER BY updated_ts DESC",
        (int(user_id),),
    ).fetchall()
    conn.close()
    return [
        {
            "id": str(r[0]),
            "created": int(r[1]),
            "updated": int(r[2]),
            "summary": str(r[3]),
        }
        for r in rows
    ]


def create_conversation(db_path: str, *, user_id: int) -> Dict[str, Any]:
    cid = new_conversation_id()
    now = _now()
    payload = {
        "id": cid,
        "created": now,
        "updated": now,
        "summary": "",
        "messages": [],
    }
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO user_conversations(id,user_id,created_ts,updated_ts,summary,messages_json) VALUES(?,?,?,?,?,?)",
        (cid, int(user_id), now, now, "", json.dumps([], ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return payload


def get_conversation(db_path: str, *, user_id: int, conversation_id: str) -> Optional[Dict[str, Any]]:
    conn = _db(db_path)
    row = conn.execute(
        "SELECT id, created_ts, updated_ts, summary, messages_json FROM user_conversations WHERE id=? AND user_id=?",
        (conversation_id, int(user_id)),
    ).fetchone()
    conn.close()
    if not row:
        return None
    cid, created, updated, summary, messages_json = row
    try:
        messages = json.loads(messages_json) if messages_json else []
    except Exception:
        messages = []
    if not isinstance(messages, list):
        messages = []
    return {
        "id": str(cid),
        "created": int(created),
        "updated": int(updated),
        "summary": str(summary or ""),
        "messages": messages,
    }


def append_message(db_path: str, *, user_id: int, conversation_id: str, msg: Dict[str, Any]) -> Dict[str, Any]:
    convo = get_conversation(db_path, user_id=user_id, conversation_id=conversation_id)
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
    for k in ["type", "url", "backend", "model", "reason", "mime", "sha256", "filename", "bytes", "attachments"]:
        if k in msg and msg.get(k) is not None:
            entry[k] = msg.get(k)

    messages = convo.get("messages")
    if not isinstance(messages, list):
        messages = []
    messages.append(entry)
    now = _now()

    conn = _db(db_path)
    conn.execute(
        "UPDATE user_conversations SET messages_json=?, updated_ts=? WHERE id=? AND user_id=?",
        (json.dumps(messages, ensure_ascii=False), now, conversation_id, int(user_id)),
    )
    conn.commit()
    conn.close()
    convo["messages"] = messages
    convo["updated"] = now
    return convo
