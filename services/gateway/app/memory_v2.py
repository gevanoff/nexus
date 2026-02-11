from __future__ import annotations

import json
import os
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from array import array

MemoryType = Literal["fact", "preference", "project", "ephemeral"]
MemorySource = Literal["user", "system", "tool"]


def _now_unix() -> int:
    return int(time.time())


def _db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def pack_emb(vec: List[float]) -> bytes:
    a = array("f", [float(x) for x in vec])
    return a.tobytes()


def unpack_emb(blob: bytes) -> List[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: List[float], b: List[float]) -> float:
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
    return dot / ((na ** 0.5) * (nb ** 0.5))


def init(db_path: str) -> None:
    conn = _db(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_v2 (
          id TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          source TEXT NOT NULL,
          text TEXT NOT NULL,
          meta TEXT,
          emb BLOB NOT NULL,
          dim INTEGER NOT NULL,
          ts INTEGER NOT NULL,
          compacted_into TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_v2_ts ON memory_v2(ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_v2_type_ts ON memory_v2(type, ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_v2_compacted ON memory_v2(compacted_into);")
    conn.commit()
    conn.close()


Embedder = Callable[[str], "list[float]"]


def upsert(
    *,
    db_path: str,
    embed: Embedder,
    text: str,
    mtype: MemoryType,
    source: MemorySource,
    meta: Optional[Dict[str, Any]] = None,
    mid: Optional[str] = None,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    meta = meta or {}
    if mid is None:
        mid = hashlib.sha256((mtype + ":" + text).encode("utf-8")).hexdigest()[:16]
    if ts is None:
        ts = _now_unix()

    emb = embed(text)
    blob = pack_emb(emb)

    conn = _db(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO memory_v2(id,type,source,text,meta,emb,dim,ts,compacted_into) VALUES(?,?,?,?,?,?,?,?,COALESCE((SELECT compacted_into FROM memory_v2 WHERE id=?), NULL))",
        (mid, mtype, source, text, json.dumps(meta), blob, len(emb), ts, mid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": mid, "dim": len(emb), "ts": ts}


def list_items(
    *,
    db_path: str,
    types: Optional[Sequence[MemoryType]] = None,
    sources: Optional[Sequence[MemorySource]] = None,
    since_ts: Optional[int] = None,
    max_age_sec: Optional[int] = None,
    limit: int = 50,
    include_compacted: bool = False,
) -> Dict[str, Any]:
    now = _now_unix()
    where = []
    args: List[Any] = []

    if not include_compacted:
        where.append("compacted_into IS NULL")

    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))

    if sources:
        where.append("source IN (%s)" % ",".join(["?"] * len(sources)))
        args.extend(list(sources))

    if since_ts is not None:
        where.append("ts >= ?")
        args.append(int(since_ts))

    if max_age_sec is not None:
        where.append("ts >= ?")
        args.append(int(now - int(max_age_sec)))

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _db(db_path)
    rows = conn.execute(
        f"SELECT id,type,source,text,meta,ts,compacted_into FROM memory_v2{clause} ORDER BY ts DESC LIMIT ?",
        (*args, int(limit)),
    ).fetchall()
    conn.close()

    out = []
    for (mid, mtype, source, text, meta, ts, compacted_into) in rows:
        out.append(
            {
                "id": mid,
                "type": mtype,
                "source": source,
                "text": text,
                "meta": json.loads(meta) if meta else None,
                "ts": ts,
                "compacted_into": compacted_into,
            }
        )
    return {"ok": True, "data": out}


def search(
    *,
    db_path: str,
    embed: Embedder,
    query: str,
    k: int,
    min_sim: float,
    types: Optional[Sequence[MemoryType]] = None,
    sources: Optional[Sequence[MemorySource]] = None,
    max_age_sec: Optional[int] = None,
    include_compacted: bool = False,
) -> Dict[str, Any]:
    qemb = embed(query)
    now = _now_unix()

    where = []
    args: List[Any] = []

    if not include_compacted:
        where.append("compacted_into IS NULL")

    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))

    if sources:
        where.append("source IN (%s)" % ",".join(["?"] * len(sources)))
        args.extend(list(sources))

    if max_age_sec is not None:
        where.append("ts >= ?")
        args.append(int(now - int(max_age_sec)))

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _db(db_path)
    rows = conn.execute(f"SELECT id,type,source,text,meta,emb,dim,ts FROM memory_v2{clause}", args).fetchall()
    conn.close()

    scored: List[Tuple[float, str, str, str, str, int]] = []
    for (mid, mtype, source, text, meta, emb_blob, dim, ts) in rows:
        if dim != len(qemb):
            continue
        emb = unpack_emb(emb_blob)
        s = cosine(qemb, emb)
        if s >= min_sim:
            scored.append((s, mid, mtype, source, text, ts))

    scored.sort(key=lambda x: x[0], reverse=True)

    out = []
    for (s, mid, mtype, source, text, ts) in scored[:k]:
        out.append({"score": float(s), "id": mid, "type": mtype, "source": source, "text": text, "ts": ts})

    return {"ok": True, "results": out}


def mark_compacted(
    *,
    db_path: str,
    ids: Sequence[str],
    into_id: str,
) -> None:
    if not ids:
        return
    conn = _db(db_path)
    conn.execute(
        "UPDATE memory_v2 SET compacted_into=? WHERE id IN (%s)" % ",".join(["?"] * len(ids)),
        (into_id, *list(ids)),
    )
    conn.commit()
    conn.close()


def delete_items(*, db_path: str, ids: Sequence[str]) -> Dict[str, Any]:
    ids = [x for x in ids if isinstance(x, str) and x.strip()]
    if not ids:
        return {"ok": True, "deleted": 0}
    conn = _db(db_path)
    cur = conn.execute("DELETE FROM memory_v2 WHERE id IN (%s)" % ",".join(["?"] * len(ids)), tuple(ids))
    conn.commit()
    deleted = int(getattr(cur, "rowcount", 0) or 0)
    conn.close()
    return {"ok": True, "deleted": deleted}
