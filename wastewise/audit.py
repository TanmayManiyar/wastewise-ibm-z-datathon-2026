"""Tamper-evident audit log.

Each entry stores the SHA-256 of (previous hash + canonical JSON of the entry),
so editing or deleting any row breaks the chain from that row onwards.
verify() recomputes the whole chain.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db

GENESIS = "0" * 64


def _digest(prev: str, ts: int, user: str | None, action: str, detail: str) -> str:
    body = json.dumps([ts, user, action, detail], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _append(conn: sqlite3.Connection, user: str | None, action: str, detail: dict[str, Any] | None) -> int:
    ts = db.now()
    d = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str)
    row = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = row[0] if row else GENESIS
    h = _digest(prev, ts, user, action, d)
    cur = conn.execute("INSERT INTO audit(ts, user, action, detail, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                       (ts, user, action, d, prev, h))
    return int(cur.lastrowid)


def log(conn: sqlite3.Connection, user: str | None, action: str, detail: dict[str, Any] | None = None) -> int:
    """Append an entry. Joins the caller's transaction if one is open."""
    if conn.in_transaction:
        return _append(conn, user, action, detail)
    with db.tx(conn):
        return _append(conn, user, action, detail)


def verify(conn: sqlite3.Connection) -> dict:
    prev = GENESIS
    n = 0
    for r in conn.execute("SELECT id, ts, user, action, detail, prev_hash, hash FROM audit ORDER BY id"):
        n += 1
        if r["prev_hash"] != prev or _digest(prev, r["ts"], r["user"], r["action"], r["detail"]) != r["hash"]:
            return {"ok": False, "checked": n, "first_bad_id": r["id"]}
        prev = r["hash"]
    return {"ok": True, "checked": n, "first_bad_id": None, "head": prev}


def recent(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    return db.rows(conn, "SELECT id, ts, user, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,))
