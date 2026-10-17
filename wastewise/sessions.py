"""Stay signed in: a signed session cookie.

Streamlit forgets the signed-in user when the page is reloaded or the phone's browser drops the
connection (screen lock). After sign-in the browser keeps a cookie

    ww_session = <base64url JSON {u, iat, exp}> . <HMAC-SHA256 of that part>

signed with a random server key (secrets/session_key, owner-readable only). A new browser session
reads the cookie, checks the signature, the expiry, that the account is still active and that the
user has not signed out since the cookie was issued. Lifetime: one crew shift (12 h) for crew
accounts, one office day (8 h) for everyone else. Signing out clears the cookie and also ends every
other cookie issued earlier to the same account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from . import config, db

COOKIE = "ww_session"
LIFETIME_S = {"crew": 12 * 3600}
DEFAULT_LIFETIME_S = 8 * 3600


def lifetime(role: str) -> int:
    return LIFETIME_S.get(role, DEFAULT_LIFETIME_S)


def _key(secrets_dir: Path | None = None) -> bytes:
    path = Path(secrets_dir or config.SECRETS_DIR) / "session_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return bytes.fromhex(path.read_text(encoding="ascii").strip())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ms(now_ts: int | None) -> int:
    return int(time.time() * 1000) if now_ts is None else int(now_ts) * 1000


def issue(username: str, role: str, now_ts: int | None = None, secrets_dir: Path | None = None) -> tuple[str, int]:
    """Returns (cookie value, max-age in seconds)."""
    t = now_ts or db.now()
    life = lifetime(role)
    # issued-at in milliseconds, so a sign-in right after a sign-out is not caught by the revocation
    body = _b64(json.dumps({"u": username, "iat": _ms(now_ts), "exp": t + life}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(secrets_dir), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", life


def check(conn: sqlite3.Connection, token: str | None, now_ts: int | None = None,
          secrets_dir: Path | None = None) -> dict | None:
    """The signed-in user for a cookie value, or None if it is forged, expired or revoked."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    want = _b64(hmac.new(_key(secrets_dir), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        d = json.loads(_unb64(body))
        username, iat, exp = str(d["u"]), int(d["iat"]), int(d["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now_ts or db.now()) >= exp:
        return None
    if iat < int(db.get_meta(conn, f"signed_out:{username}", "0") or 0):
        return None
    u = db.one(conn, "SELECT username, display_name, role, vehicle_id, active FROM users WHERE username=?", (username,))
    if not u or not u["active"]:
        return None
    if exp - iat // 1000 > lifetime(u["role"]):       # role changed to one with a shorter lifetime
        return None
    return {k: u[k] for k in ("username", "display_name", "role", "vehicle_id")} | {"session_exp": exp}


def revoke(conn: sqlite3.Connection, username: str, now_ts: int | None = None) -> None:
    """Sign-out: every cookie issued to this account up to now stops working."""
    with db.tx(conn):
        db.set_meta(conn, f"signed_out:{username}", str(_ms(now_ts) + 1))
