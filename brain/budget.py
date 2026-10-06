"""Spending caps: daily_budget_usd and weekly_budget_usd ([model]).

Both are counted in the [agent] timezone: a day starts at local midnight, a
week at Monday 00:00 local. Spend is every traced step's cost_usd since that
start. Either cap reached stops new turns; a turn already running finishes.
0 disables a cap. Messages that arrive while a cap is reached are held in the
inbox (brain/inbox.py), never dropped, and answered once spend is back under
both caps (the period rolls over or the owner raises a cap)."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo


def _zone(cfg) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def periods(cfg, now: float | None = None) -> dict:
    """{"day"|"week": (start_ts, end_ts)} for the periods containing `now`."""
    tz = _zone(cfg)
    local = dt.datetime.fromtimestamp(time.time() if now is None else now, tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - dt.timedelta(days=day.weekday())
    # add calendar days, then re-localize: a DST shift can't skew the edges
    nxt = lambda d, n: (d.replace(tzinfo=None) + dt.timedelta(days=n)
                        ).replace(tzinfo=tz)
    return {"day": (day.timestamp(), nxt(day, 1).timestamp()),
            "week": (week.timestamp(), nxt(week, 7).timestamp())}


def caps(cfg) -> dict:
    return {"day": float(cfg.get("daily_budget_usd") or 0),
            "week": float(cfg.get("weekly_budget_usd") or 0)}


def status(cfg, db, now: float | None = None) -> list[dict]:
    """One entry per enabled cap: period, limit, spent, start, resets_at."""
    out = []
    span = periods(cfg, now)
    for period, limit in caps(cfg).items():
        if limit <= 0:
            continue
        start, end = span[period]
        out.append({"period": period, "limit": limit,
                    "spent": db.spend_since(start), "start": start,
                    "resets_at": end})
    return out


def reached(cfg, db, now: float | None = None) -> dict | None:
    """The cap that blocks new turns now, or None. With both reached, the
    one that resets last (that is when turns resume)."""
    over = [s for s in status(cfg, db, now) if s["spent"] > s["limit"]]
    return max(over, key=lambda s: s["resets_at"]) if over else None


def describe(cfg, cap: dict) -> dict:
    """Fill-ins for the budget_reached message."""
    tz = _zone(cfg)
    resets = dt.datetime.fromtimestamp(cap["resets_at"], tz)
    pt = str(cfg.get("language", "")).lower().startswith("portug")
    names = ({"day": "diário", "week": "semanal"} if pt
             else {"day": "daily", "week": "weekly"})
    return {"period": names[cap["period"]], "limit": f"{cap['limit']:.2f}",
            "spent": f"{cap['spent']:.2f}",
            "resets": resets.strftime("%d/%m %H:%M" if pt else "%a %d %b %H:%M")}
