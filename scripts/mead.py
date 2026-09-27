#!/usr/bin/env python3
"""Tiny CLI for the Mead Tracker API. Reads MEAD_API_TOKEN from
~/.config/mead-tracker/env (or the environment).

  mead.py batches [--status active]
  mead.py show <id>
  mead.py reading <id> <gravity> [--date YYYY-MM-DD] [--temp F] [--notes ...]
  mead.py nutrient <id> "<type>" "<amount>" [--date ...] [--notes ...]
  mead.py event <id> <type> [--qt N | --gal N | --cups N] [--sugar-oz N]
                [--sg-before G] [--sg-after G] [--vol-after GAL] [--date ...] [--notes ...]
  mead.py tasting <id> [--rating N] [--aroma ...] [--flavor ...] [--notes ...]
  mead.py set <id> field=value [field=value ...]

MEAD_URL overrides the base URL (default http://127.0.0.1:8789).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _env():
    path = os.path.expanduser("~/.config/mead-tracker/env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v.strip("'\""))


def call(method, path, body=None):
    base = os.environ.get("MEAD_URL", "http://127.0.0.1:8789").rstrip("/")
    req = urllib.request.Request(
        f"{base}/api{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['MEAD_API_TOKEN']}",
            "Content-Type": "application/json",
            # Cloudflare's bot check 403s the default Python-urllib UA
            "User-Agent": "mead-cli/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")


def main():
    _env()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("batches"); s.add_argument("--status")
    s = sub.add_parser("show"); s.add_argument("id", type=int)
    s = sub.add_parser("reading"); s.add_argument("id", type=int); s.add_argument("gravity", type=float)
    s.add_argument("--date"); s.add_argument("--temp", type=float); s.add_argument("--notes", default="")
    s = sub.add_parser("nutrient"); s.add_argument("id", type=int); s.add_argument("type"); s.add_argument("amount")
    s.add_argument("--date"); s.add_argument("--notes", default="")
    s = sub.add_parser("event"); s.add_argument("id", type=int); s.add_argument("type")
    for f in ("--qt", "--gal", "--cups", "--sugar-oz", "--sg-before", "--sg-after", "--vol-after"):
        s.add_argument(f, type=float)
    s.add_argument("--date"); s.add_argument("--amount", default=""); s.add_argument("--notes", default="")
    s = sub.add_parser("tasting"); s.add_argument("id", type=int); s.add_argument("--rating", type=int)
    for f in ("--aroma", "--flavor", "--body", "--sweetness", "--notes", "--date"):
        s.add_argument(f)
    s = sub.add_parser("set"); s.add_argument("id", type=int); s.add_argument("pairs", nargs="+")
    a = p.parse_args()

    if a.cmd == "batches":
        out = call("GET", "/batches" + (f"?status={a.status}" if a.status else ""))
    elif a.cmd == "show":
        out = call("GET", f"/batch/{a.id}")
    elif a.cmd == "reading":
        out = call("POST", f"/batch/{a.id}/readings",
                   {"gravity": a.gravity, "date": a.date, "temperature_f": a.temp, "notes": a.notes})
    elif a.cmd == "nutrient":
        out = call("POST", f"/batch/{a.id}/nutrients",
                   {"nutrient_type": a.type, "amount": a.amount, "date": a.date, "notes": a.notes})
    elif a.cmd == "event":
        out = call("POST", f"/batch/{a.id}/events", {
            "event_type": a.type, "date": a.date, "amount": a.amount, "notes": a.notes,
            "volume_added_qt": a.qt, "volume_added_gal": a.gal, "volume_added_cups": a.cups,
            "sugar_oz": a.sugar_oz, "gravity_before": a.sg_before, "gravity_after": a.sg_after,
            "volume_after_gal": a.vol_after,
        })
    elif a.cmd == "tasting":
        out = call("POST", f"/batch/{a.id}/tastings", {
            "overall_rating": a.rating, "aroma": a.aroma, "flavor": a.flavor, "body": a.body,
            "sweetness": a.sweetness, "notes": a.notes, "date": a.date,
        })
    elif a.cmd == "set":
        body = dict(pair.split("=", 1) for pair in a.pairs)
        out = call("PATCH", f"/batch/{a.id}", body)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
