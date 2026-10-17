"""Users, roles, passwords and API keys.

Passwords: PBKDF2-HMAC-SHA256, 16-byte random salt, 600 000 iterations
(OWASP 2023 guidance). On IBM Z, OpenSSL's SHA-256 uses the CPACF crypto
instructions. Initial passwords are generated randomly and written only to
secrets/initial_passwords.txt (owner-readable); they are never shown in the UI.
API keys are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from . import audit, config, db

ITERATIONS = 600_000
MAX_FAILED = 5
LOCK_SECONDS = 300

ROLES = {
    "admin": "Administrator",
    "supervisor": "Ward supervisor",
    "operator": "Control-room operator",
    "viewer": "Viewer",
    "crew": "Collection crew",
}

# What each role may do. Crews only see and update their own route.
PERMISSIONS = {
    "view_office": {"admin", "supervisor", "operator", "viewer"},
    "generate_plan": {"admin", "supervisor", "operator"},
    "approve_plan": {"admin", "supervisor"},
    "replan": {"admin", "supervisor", "operator"},
    "record_progress": {"admin", "supervisor", "operator", "crew"},
    "complaints": {"admin", "supervisor", "operator"},
    "edit_bins": {"admin", "supervisor"},
    "edit_fleet": {"admin"},
    "manage_users": {"admin"},
    "settings": {"admin"},
    "demo_tools": {"admin"},
    "whatif": {"admin", "supervisor", "operator"},
    "reports": {"admin", "supervisor", "operator", "viewer"},
}


def can(role: str | None, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> tuple[str, str, int]:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), dk.hex(), iterations


def check_password(password: str, salt_hex: str, hash_hex: str, iterations: int) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations)
    return hmac.compare_digest(dk.hex(), hash_hex)


def generate_password() -> str:
    return secrets.token_urlsafe(12)


def create_user(conn: sqlite3.Connection, username: str, display_name: str, role: str,
                vehicle_id: str | None = None, password: str | None = None,
                actor: str | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    if role == "crew" and not vehicle_id:
        raise ValueError("crew accounts need a vehicle")
    password = password or generate_password()
    salt, h, it = hash_password(password)
    with db.tx(conn):
        conn.execute("INSERT INTO users(username, display_name, role, vehicle_id, salt, pw_hash, iterations,"
                     " active, created_at) VALUES (?,?,?,?,?,?,?,1,?)",
                     (username, display_name, role, vehicle_id, salt, h, it, db.now()))
        audit.log(conn, actor, "user_created", {"username": username, "role": role, "vehicle": vehicle_id})
    return password


def set_password(conn: sqlite3.Connection, username: str, password: str, actor: str | None = None) -> None:
    salt, h, it = hash_password(password)
    with db.tx(conn):
        conn.execute("UPDATE users SET salt=?, pw_hash=?, iterations=?, failed_logins=0, locked_until=NULL"
                     " WHERE username=?", (salt, h, it, username))
        audit.log(conn, actor or username, "password_changed", {"username": username})


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """Returns the user row (without secrets) or None. Locks after repeated failures."""
    u = db.one(conn, "SELECT * FROM users WHERE username = ?", (username.strip(),))
    t = db.now()
    if not u or not u["active"]:
        # Same work for unknown users so timing does not reveal valid names.
        check_password(password, "00" * 16, "00" * 32, ITERATIONS)
        audit.log(conn, username, "login_failed", {"reason": "unknown or inactive"})
        return None
    if u["locked_until"] and u["locked_until"] > t:
        audit.log(conn, username, "login_failed", {"reason": "locked"})
        return None
    if not check_password(password, u["salt"], u["pw_hash"], u["iterations"]):
        with db.tx(conn):
            n = u["failed_logins"] + 1
            lock = t + LOCK_SECONDS if n >= MAX_FAILED else None
            conn.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE username=?",
                         (0 if lock else n, lock, username))
            audit.log(conn, username, "login_failed", {"reason": "bad password", "locked": bool(lock)})
        return None
    with db.tx(conn):
        conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE username=?", (username,))
        audit.log(conn, username, "login", {"role": u["role"]})
    return {k: u[k] for k in ("username", "display_name", "role", "vehicle_id")}


def users(conn: sqlite3.Connection) -> list[dict]:
    return db.rows(conn, "SELECT username, display_name, role, vehicle_id, active, created_at FROM users"
                         " ORDER BY role, username")


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def seed_users(conn: sqlite3.Connection, secrets_dir: Path | None = None) -> Path | None:
    """Create the default accounts once; passwords go to secrets/initial_passwords.txt."""
    if db.one(conn, "SELECT COUNT(*) AS n FROM users")["n"]:
        return None
    secrets_dir = Path(secrets_dir or config.SECRETS_DIR)
    accounts = [("admin", "Administrator", "admin", None),
                ("supervisor", "Ward supervisor (Sanpada)", "supervisor", None),
                ("operator", "Control-room operator", "operator", None),
                ("viewer", "Viewer", "viewer", None)]
    for v in db.vehicles(conn):
        n = v["id"].split("-")[-1]
        accounts.append((f"crew{n}", f"Crew - {v['name']}", "crew", v["id"]))
    lines = ["# WasteWise initial passwords - generated on first start. Change them after first sign-in.",
             "# Keep this file private (owner read/write only).", ""]
    for username, display, role, vid in accounts:
        pw = create_user(conn, username, display, role, vid, actor="system")
        lines.append(f"{username:12s} {role:11s} {pw}" + (f"   vehicle {vid}" if vid else ""))
    path = secrets_dir / "initial_passwords.txt"
    _write_secret(path, "\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- API keys
def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_api_key(conn: sqlite3.Connection, name: str, actor: str | None = None) -> str:
    key = "ww_" + secrets.token_urlsafe(32)
    with db.tx(conn):
        conn.execute("INSERT INTO api_keys(name, key_hash, created_at, active) VALUES (?,?,?,1)",
                     (name, _key_hash(key), db.now()))
        audit.log(conn, actor, "api_key_created", {"name": name})
    return key


def check_api_key(conn: sqlite3.Connection, key: str | None) -> str | None:
    """Returns the key's name if valid."""
    if not key:
        return None
    r = db.one(conn, "SELECT name FROM api_keys WHERE key_hash = ? AND active = 1", (_key_hash(key),))
    return r["name"] if r else None


def seed_api_key(conn: sqlite3.Connection, secrets_dir: Path | None = None) -> Path | None:
    if db.one(conn, "SELECT COUNT(*) AS n FROM api_keys")["n"]:
        return None
    key = create_api_key(conn, "default ingestion key", actor="system")
    path = Path(secrets_dir or config.SECRETS_DIR) / "api_key.txt"
    _write_secret(path, key + "\n")
    return path
