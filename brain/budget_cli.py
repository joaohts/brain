"""Spending caps from a terminal: the door for local agents (and the owner).

  brain-budget status [--json]
  brain-budget set day 5 --by NAME [--reason TEXT]       until next 00:00
  brain-budget set day +2 --by NAME [--reason TEXT]      on top of the current cap
  brain-budget set week 15 --by NAME [--reason TEXT]     until next Monday 00:00
  brain-budget cancel day|week --by NAME
  brain-budget log [--limit N] [--json]

Same API as the owner's WhatsApp commands (brain/budget.py): a temporary
override for the rest of the current period, never an edit of config.toml.
The running brain reads it at its next check (no restart): held messages are
answered in order once both caps allow, and alerts re-arm. Exit status: 0 ok,
1 refused, 2 usage.

Limits for this door (the owner set them, agents can't change them here):
the cap it sets may not exceed [model] budget_cli_max_daily_usd /
budget_cli_max_weekly_usd (0 = no raises from the CLI); --by is required and
stored with source "cli", the reason, the OS user and the parent process; the
owner gets a WhatsApp notice of every change made here."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys

from . import budget, config
from .db import DB

PERIOD = {"day": "day", "dia": "day", "hoje": "day", "today": "day",
          "week": "week", "semana": "week"}


def _who(by: str) -> str:
    """--by plus what the OS can vouch for (user, parent process, comms
    alias). The CLI can't authenticate an agent; this is for the audit."""
    try:
        with open(f"/proc/{os.getppid()}/comm") as f:
            parent = f.read().strip()
    except OSError:
        parent = "?"
    extra = f"user {getpass.getuser()}, ppid {os.getppid()} {parent}"
    if os.environ.get("COMMS_AGENT"):
        extra += f", COMMS_AGENT {os.environ['COMMS_AGENT']}"
    return f"{by.strip()} ({extra})"


def _iso(cfg, ts):
    return dt.datetime.fromtimestamp(ts, budget._zone(cfg)).isoformat(
        timespec="minutes") if ts else None


def _status_json(cfg, db) -> dict:
    out = []
    for s in budget.status(cfg, db):
        o = s["override"]
        out.append({"period": s["period"], "limit_usd": s["limit"],
                    "base_usd": s["base"], "spent_usd": round(s["spent"], 4),
                    "left_usd": round(s["limit"] - s["spent"], 4),
                    "reached": s["spent"] > s["limit"],
                    "resets_at": _iso(cfg, s["resets_at"]),
                    "override": o and {"id": o["id"], "limit_usd": o["limit_usd"],
                                       "until": _iso(cfg, o["valid_until"]),
                                       "source": o["source"], "by": o["by"],
                                       "reason": o["reason"]}})
    b = budget.binding(cfg, db)
    return {"caps": out, "binding": b and b["period"],
            "cli_max_usd": {"day": budget.cli_ceiling(cfg, "day"),
                            "week": budget.cli_ceiling(cfg, "week")}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="brain-budget",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="spend, effective caps, overrides")
    st.add_argument("--json", action="store_true")
    s = sub.add_parser("set", help="temporary cap for the current period")
    s.add_argument("period", choices=sorted(PERIOD))
    s.add_argument("value", help='amount in USD ("5") or increment ("+2")')
    s.add_argument("--by", required=True, help="who asks (agent/session name)")
    s.add_argument("--reason", default="")
    c = sub.add_parser("cancel", help="back to the base cap")
    c.add_argument("period", choices=sorted(PERIOD))
    c.add_argument("--by", required=True)
    c.add_argument("--reason", default="")
    lg = sub.add_parser("log", help="audit trail of overrides")
    lg.add_argument("--limit", type=int, default=20)
    lg.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    cfg = config.load()
    db = DB(cfg["db_path"])
    if a.cmd == "status":
        print(json.dumps(_status_json(cfg, db), ensure_ascii=False, indent=1)
              if a.json else budget.status_text(cfg, db))
        return 0
    if a.cmd == "log":
        rows = budget.history(db, a.limit)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        for r in [] if a.json else rows:
            state = (f"cancelled {_iso(cfg, r['cancelled_at'])} by "
                     f"{r['cancelled_by']}" if r["cancelled_at"] else "")
            print(f"#{r['id']} {_iso(cfg, r['created_at'])} {r['period']} "
                  f"US$ {r['limit_usd']:.2f} until {_iso(cfg, r['valid_until'])}"
                  f" via {r['source']} by {r['by']}"
                  f"{' — ' + r['reason'] if r['reason'] else ''}"
                  f"{' [' + state + ']' if state else ''}")
        return 0
    period = PERIOD[a.period]
    who = _who(a.by)
    try:
        if a.cmd == "set":
            row = budget.set_override(cfg, db, period, a.value, source="cli",
                                      by=who, channel="cli", reason=a.reason)
            text = budget.change_text(cfg, row)
        else:
            n = budget.cancel_override(cfg, db, period, source="cli", by=who,
                                       channel="cli")
            if not n:
                print(budget.cancel_text(cfg, period, 0))
                return 0
            text = budget.cancel_text(cfg, period, n)
    except budget.BudgetError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    pt = budget._pt(cfg)
    budget.announce(cfg, db, (f"🔧 Orçamento alterado pela CLI ({a.by}"
                              f"{': ' + a.reason if a.reason else ''}). "
                              if pt else
                              f"🔧 Budget changed from the CLI ({a.by}"
                              f"{': ' + a.reason if a.reason else ''}). ")
                    + text + (' Desfazer: "orçamento cancelar '
                              f'{"hoje" if period == "day" else "semana"}".'
                              if pt else ""))
    print(text)
    print(budget.status_text(cfg, db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
