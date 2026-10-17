"""Command line: python -m wastewise.cli <command>

  init           create the database, download OSM (if missing), set up the
                 area, accounts, API key and synthetic demonstration history
  refresh-demo   regenerate synthetic history up to now (keeps manual records,
                 settings, bins, fleet, users)
  verify-audit   check the hash chain of the audit log
  platform       print machine / Telum information
"""
from __future__ import annotations

import argparse
import json
import sys

from . import area, audit, auth, config, db, osm, platform_info, synth


def cmd_init(args) -> None:
    config.ensure_dirs()
    conn = db.connect()
    db.init(conn)
    if args.download or not (config.DATA_DIR / osm.ROADS_FILE).exists():
        print("Downloading OpenStreetMap extract via Overpass ...")
        osm.download(force=args.download)
    if not db.bins(conn, active_only=False):
        print("Area:", area.setup(conn))
    p = auth.seed_users(conn)
    if p:
        print("Initial passwords written to", p)
    k = auth.seed_api_key(conn)
    if k:
        print("API key written to", k)
    if args.synthetic and not db.get_meta(conn, "synthetic_info"):
        info = synth.generate(conn, days=args.days)
        audit.log(conn, "system", "synthetic_generated", info)
        print("Synthetic demonstration history:", json.dumps(info["rows"]))
        from . import crews
        crews.seed_demo(conn)
    print("Database:", config.DB_PATH)


def cmd_refresh(args) -> None:
    conn = db.connect()
    db.init(conn)
    info = synth.generate(conn, days=args.days)
    audit.log(conn, args.user, "synthetic_refreshed", info)
    print(json.dumps(info, indent=2))


def cmd_verify(args) -> None:
    res = audit.verify(db.connect())
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["ok"] else 1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="wastewise")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--download", action="store_true", help="re-download OSM even if present")
    p.add_argument("--no-synthetic", dest="synthetic", action="store_false")
    p.add_argument("--days", type=int, default=180)
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("refresh-demo")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--user", default="cli")
    p.set_defaults(fn=cmd_refresh)
    sub.add_parser("verify-audit").set_defaults(fn=cmd_verify)
    sub.add_parser("platform").set_defaults(fn=lambda a: print(json.dumps(platform_info.detect(), indent=2)))
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
