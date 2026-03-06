from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import hashlib
import hmac
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_api_keys (
          id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          token_hash TEXT UNIQUE NOT NULL,
          token_hint TEXT NOT NULL,
          created_ts INTEGER NOT NULL,
          updated_ts INTEGER NOT NULL,
          last_used_ts INTEGER,
          revoked_ts INTEGER,
          expires_ts INTEGER,
          policy_json TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_conversations_user ON user_conversations(user_id, updated_ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id, created_ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_hash ON user_api_keys(token_hash);")
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


def _hash_api_key(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _new_api_key_token() -> str:
    return "gk_" + secrets.token_urlsafe(40).replace("-", "_")


def _api_key_hint(token: str) -> str:
    value = (token or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:8]}...{value[-4:]}"


def create_api_key(
    db_path: str,
    *,
    user_id: int,
    name: str,
    expires_ts: Optional[int] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    label = (name or "").strip()
    if not label:
        raise ValueError("name required")

    now = _now()
    key_id = secrets.token_urlsafe(12).replace("-", "_")
    raw_token = _new_api_key_token()
    token_hash = _hash_api_key(raw_token)
    token_hint = _api_key_hint(raw_token)
    policy_obj = policy if isinstance(policy, dict) else {}

    conn = _db(db_path)
    try:
        exists = conn.execute("SELECT id FROM users WHERE id=?", (int(user_id),)).fetchone()
        if not exists:
            raise ValueError("user not found")

        conn.execute(
            """
            INSERT INTO user_api_keys(id,user_id,name,token_hash,token_hint,created_ts,updated_ts,last_used_ts,revoked_ts,expires_ts,policy_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key_id,
                int(user_id),
                label,
                token_hash,
                token_hint,
                now,
                now,
                None,
                None,
                int(expires_ts) if expires_ts is not None else None,
                json.dumps(policy_obj, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "id": key_id,
        "name": label,
        "token": raw_token,
        "token_hint": token_hint,
        "created_ts": now,
        "expires_ts": int(expires_ts) if expires_ts is not None else None,
        "revoked": False,
        "policy": policy_obj,
    }


def list_api_keys(db_path: str, *, user_id: int) -> List[Dict[str, Any]]:
    conn = _db(db_path)
    rows = conn.execute(
        """
        SELECT id, name, token_hint, created_ts, updated_ts, last_used_ts, revoked_ts, expires_ts, policy_json
        FROM user_api_keys
        WHERE user_id=?
        ORDER BY created_ts DESC
        """,
        (int(user_id),),
    ).fetchall()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        policy: Dict[str, Any] = {}
        try:
            parsed = json.loads(r[8] or "{}")
            if isinstance(parsed, dict):
                policy = parsed
        except Exception:
            policy = {}
        out.append(
            {
                "id": str(r[0]),
                "name": str(r[1] or ""),
                "token_hint": str(r[2] or ""),
                "created_ts": int(r[3] or 0),
                "updated_ts": int(r[4] or 0),
                "last_used_ts": int(r[5]) if r[5] is not None else None,
                "revoked": r[6] is not None,
                "revoked_ts": int(r[6]) if r[6] is not None else None,
                "expires_ts": int(r[7]) if r[7] is not None else None,
                "policy": policy,
            }
        )
    return out


def revoke_api_key(db_path: str, *, user_id: int, key_id: str) -> bool:
    now = _now()
    conn = _db(db_path)
    cur = conn.execute(
        """
        UPDATE user_api_keys
        SET revoked_ts=?, updated_ts=?
        WHERE id=? AND user_id=? AND revoked_ts IS NULL
        """,
        (now, now, str(key_id), int(user_id)),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_user_by_api_key(db_path: str, *, token: str, touch_last_used: bool = True) -> Optional[tuple[User, Dict[str, Any]]]:
    raw = (token or "").strip()
    if not raw:
        return None
    token_hash = _hash_api_key(raw)

    now = _now()
    conn = _db(db_path)
    row = conn.execute(
        """
        SELECT
          u.id, u.username, u.disabled, u.admin,
          k.id, k.name, k.token_hint, k.created_ts, k.updated_ts, k.last_used_ts, k.revoked_ts, k.expires_ts, k.policy_json, k.token_hash
        FROM user_api_keys k
        JOIN users u ON u.id = k.user_id
        WHERE k.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()

    if not row:
        conn.close()
        return None

    (
        user_id,
        username,
        disabled,
        admin,
        key_id,
        key_name,
        token_hint,
        created_ts,
        updated_ts,
        last_used_ts,
        revoked_ts,
        expires_ts,
        policy_json,
        stored_hash,
    ) = row

    if not hmac.compare_digest(str(stored_hash or ""), token_hash):
        conn.close()
        return None
    if bool(disabled):
        conn.close()
        return None
    if revoked_ts is not None:
        conn.close()
        return None
    if expires_ts is not None and int(expires_ts) < now:
        conn.close()
        return None

    if touch_last_used:
        conn.execute("UPDATE user_api_keys SET last_used_ts=?, updated_ts=? WHERE id=?", (now, now, str(key_id)))
        conn.commit()
    conn.close()

    policy: Dict[str, Any] = {}
    try:
        parsed = json.loads(policy_json or "{}")
        if isinstance(parsed, dict):
            policy = parsed
    except Exception:
        policy = {}

    user = User(id=int(user_id), username=str(username), disabled=False, admin=bool(admin))
    key_meta = {
        "id": str(key_id),
        "name": str(key_name or ""),
        "token_hint": str(token_hint or ""),
        "created_ts": int(created_ts or 0),
        "updated_ts": int(updated_ts or 0),
        "last_used_ts": int(last_used_ts) if last_used_ts is not None else None,
        "revoked": False,
        "expires_ts": int(expires_ts) if expires_ts is not None else None,
        "policy": policy,
    }
    return user, key_meta


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
