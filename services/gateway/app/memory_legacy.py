from __future__ import annotations

import json
import os
import sqlite3
import time
import hashlib
import math
from array import array

from app.config import S
from app.upstreams import embed_text_for_memory


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(S.MEMORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(S.MEMORY_DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def memory_init() -> None:
    conn = _db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
          id TEXT PRIMARY KEY,
          text TEXT NOT NULL,
          meta TEXT,
          emb BLOB NOT NULL,
          dim INTEGER NOT NULL,
          ts INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(ts);")
    conn.commit()
    conn.close()


def pack_emb(vec: list[float]) -> bytes:
    a = array("f", [float(x) for x in vec])
    return a.tobytes()


def unpack_emb(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = na = nb = 0.0
    for i in range(len(a)):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def memory_upsert(text: str, meta: dict | None = None, mid: str | None = None) -> dict:
    meta = meta or {}
    if mid is None:
        mid = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # NOTE: embed_text_for_memory is async; callers should await wrapper.
    raise RuntimeError("Use memory_upsert_async")


async def memory_upsert_async(text: str, meta: dict | None = None, mid: str | None = None) -> dict:
    meta = meta or {}
    if mid is None:
        mid = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    emb = await embed_text_for_memory(text)
    blob = pack_emb(emb)

    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO memory(id,text,meta,emb,dim,ts) VALUES(?,?,?,?,?,?)",
        (mid, text, json.dumps(meta), blob, len(emb), int(time.time())),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": mid, "dim": len(emb)}


async def memory_search(query: str, k: int, min_sim: float) -> dict:
    qemb = await embed_text_for_memory(query)

    conn = _db()
    rows = conn.execute("SELECT id,text,meta,emb,dim,ts FROM memory").fetchall()
    conn.close()

    scored = []
    for (mid, text, meta, emb_blob, dim, ts) in rows:
        if dim != len(qemb):
            continue
        emb = unpack_emb(emb_blob)
        s = cosine(qemb, emb)
        if s >= min_sim:
            scored.append((s, mid, text, meta, ts))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for (s, mid, text, meta, ts) in scored[:k]:
        out.append({"score": s, "id": mid, "text": text, "meta": json.loads(meta) if meta else None, "ts": ts})
    return {"ok": True, "results": out}
