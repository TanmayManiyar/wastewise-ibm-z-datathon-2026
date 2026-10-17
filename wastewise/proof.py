"""Proof of service and truck positions.

When a crew taps Collected / Could not reach / Bin missing, the phone sends its GPS position (only with the
crew's permission, and only over HTTPS - the public link is HTTPS) and optionally a photo (required for
"Bin missing"). WasteWise stores both with the event and compares the position with the bin:
"verified, 35 m from the bin", "900 m away" or "no location".

The same positions give each truck's last known place ("last seen 07:42") and let a re-plan start each
truck from where it really is.
"""
from __future__ import annotations

import io
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from . import config, db, fmt, geo

VERIFIED_M = 100          # GPS in a town is usually good to 10-30 m; bins sit at the kerb
FIX_MAX_AGE_S = 180       # a position older than this is not used for a tap
POSITION_MAX_AGE_S = 30 * 60   # re-plan uses a GPS position only if it is this recent
PHOTO_MAX_PX = 1600


def photos_dir() -> Path:
    return config.DATA_DIR / "photos"


def save_photo(data: bytes, day: str) -> str:
    """Store a photo (shrunk to at most 1600 px, re-encoded as JPEG without metadata) and return its path
    relative to the photos folder."""
    sub = photos_dir() / day
    sub.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img = img.convert("RGB")
        img.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82, optimize=True)
        data = buf.getvalue()
    except Exception as exc:          # not an image we can read: refuse rather than store unknown files
        raise ValueError("This file is not a photo WasteWise can read (use JPEG or PNG)") from exc
    (sub / name).write_bytes(data)
    return f"{day}/{name}"


def photo_bytes(rel: str) -> bytes | None:
    p = (photos_dir() / rel).resolve()
    if photos_dir().resolve() not in p.parents or not p.exists():
        return None
    return p.read_bytes()


def usable_fix(fix: dict | None, received_ts: float | None, now_ts: float | None = None) -> dict | None:
    """The phone's position if it is recent enough to prove where a tap happened."""
    if not fix or fix.get("lat") is None or received_ts is None:
        return None
    if (now_ts or db.now()) - received_ts > FIX_MAX_AGE_S:
        return None
    return {"lat": float(fix["lat"]), "lon": float(fix["lon"]), "acc": float(fix.get("acc") or 0),
            "ts": int(received_ts)}


def distance_to_bin(conn: sqlite3.Connection, bin_id: str | None, lat: float | None, lon: float | None) -> float | None:
    if bin_id is None or lat is None:
        return None
    b = db.one(conn, "SELECT lat, lon FROM bins WHERE id=?", (bin_id,))
    return None if not b else round(geo.haversine_m(lat, lon, b["lat"], b["lon"]))


def verdict(e: dict | None) -> tuple[str, str]:
    """(colour, text) for an event: verified / far away / no location."""
    if not e or e.get("kind") not in ("collected", "could_not_reach", "bin_missing"):
        return "grey", ""
    d = e.get("dist_m")
    if d is None:
        txt = "no location"
        return "grey", txt + (" · photo" if e.get("photo") else "")
    photo = " · photo" if e.get("photo") else ""
    if d <= VERIFIED_M:
        return "green", f"verified, {int(d)} m from the bin{photo}"
    far = f"{fmt.km1(d / 1000)} away" if d >= 1000 else f"{int(round(d, -1))} m away"
    return ("orange" if d < 500 else "red"), far + photo


def last_positions(conn: sqlite3.Connection, now_ts: int | None = None, max_age_s: int = 12 * 3600) -> dict[str, dict]:
    """vehicle_id -> {"lat", "lon", "ts", "acc", "kind"} from the newest event with a GPS position."""
    now_ts = now_ts or db.now()
    out = {}
    for r in db.rows(conn, "SELECT vehicle_id, lat, lon, gps_acc_m, ts, kind FROM events WHERE lat IS NOT NULL"
                           " AND vehicle_id IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ts",
                     (now_ts - max_age_s, now_ts + 60)):
        out[r["vehicle_id"]] = {"lat": r["lat"], "lon": r["lon"], "ts": r["ts"], "acc": r["gps_acc_m"],
                                "kind": r["kind"]}
    return out


def nearest_stop(net, lat: float, lon: float, allowed: set[str] | None = None) -> tuple[int, float]:
    """Index of the network stop nearest to a position (bins, depot, landfill), and the distance in m."""
    best, best_d = 0, float("inf")
    for i, sid in enumerate(net.stop_ids):
        if allowed is not None and sid not in allowed:
            continue
        node = int(net.stop_node[i])
        d = geo.haversine_m(lat, lon, float(net.graph.lat[node]), float(net.graph.lon[node]))
        if d < best_d:
            best, best_d = i, d
    return best, best_d


def seen_text(ts: int | None) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, config.IST)
    today = datetime.now(config.IST).date()
    return f"last seen {fmt.hhmm(ts)}" + ("" if d.date() == today else f" on {fmt.when(ts).rsplit(' ', 1)[0]}")
