"""Spending caps: daily_budget_usd and weekly_budget_usd ([model]).

Both are counted in the [agent] timezone: a day starts at local midnight, a
week at Monday 00:00 local. Spend is every traced step's cost_usd since that
start. Either cap reached stops new turns; a turn already running finishes.
0 disables a cap. Messages that arrive while a cap is reached are held in the
inbox (brain/inbox.py), never dropped, and answered once spend is back under
both caps (the period rolls over or a cap is raised).

Temporary overrides (`budget_overrides`, brain DB) replace a base cap for the
rest of the current period only: a daily one until the next local midnight,
a weekly one until next Monday 00:00. config.toml is never touched; the
effective cap is read at every check, so a change applies without a restart,
survives one, and expires by itself. Rows are never deleted (a cancel stamps
cancelled_at), so every change stays auditable: who, from where, value,
expiry. One API for every door: the owner's WhatsApp commands (brain/inbox.py),
the owner-only `budget` tool and the local agent CLI (brain/budget_cli.py).

Alerts: crossing 80% and reaching 100% of each effective cap sends one plain
message to the owner (no model call), once per (cap, period, threshold,
effective limit): raising a cap re-arms the thresholds against the new value."""

from __future__ import annotations

import datetime as dt
import math
import re
import time
import unicodedata
from zoneinfo import ZoneInfo

PERIODS = ("day", "week")
THRESHOLDS = (80, 100)
SOURCES = ("whatsapp", "tool", "cli")

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL,             -- day | week
  period_start REAL NOT NULL,       -- start of the period it applies to
  limit_usd REAL NOT NULL,
  valid_until REAL NOT NULL,        -- end of that period
  source TEXT NOT NULL,             -- whatsapp | tool | cli
  by TEXT NOT NULL,                 -- who (runtime identity, or --by for cli)
  channel TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  cancelled_at REAL,
  cancelled_by TEXT
);
CREATE INDEX IF NOT EXISTS budget_overrides_period
  ON budget_overrides(period, valid_until);
CREATE TABLE IF NOT EXISTS budget_alerts (
  period TEXT NOT NULL,
  period_start REAL NOT NULL,
  threshold INTEGER NOT NULL,
  limit_usd REAL NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY (period, period_start, threshold, limit_usd)
);
"""


class BudgetError(ValueError):
    """A refused change; the message is safe to show to whoever asked."""


def _zone(cfg) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _pt(cfg) -> bool:
    return str(cfg.get("language", "")).lower().startswith("portug")


def _now(now):
    return time.time() if now is None else now


def periods(cfg, now: float | None = None) -> dict:
    """{"day"|"week": (start_ts, end_ts)} for the periods containing `now`."""
    tz = _zone(cfg)
    local = dt.datetime.fromtimestamp(_now(now), tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - dt.timedelta(days=day.weekday())
    # add calendar days, then re-localize: a DST shift can't skew the edges
    nxt = lambda d, n: (d.replace(tzinfo=None) + dt.timedelta(days=n)
                        ).replace(tzinfo=tz)
    return {"day": (day.timestamp(), nxt(day, 1).timestamp()),
            "week": (week.timestamp(), nxt(week, 7).timestamp())}


def base_caps(cfg) -> dict:
    return {"day": float(cfg.get("daily_budget_usd") or 0),
            "week": float(cfg.get("weekly_budget_usd") or 0)}


def override(db, period: str, now: float | None = None) -> dict | None:
    """The live override for `period` at `now` (the newest one), or None."""
    now = _now(now)
    r = db.conn.execute(
        "SELECT * FROM budget_overrides WHERE period=? AND cancelled_at IS NULL "
        "AND period_start<=? AND valid_until>? ORDER BY id DESC LIMIT 1",
        (period, now, now)).fetchone()
    if r is None:
        return None
    cols = [d[0] for d in db.conn.execute(
        "SELECT * FROM budget_overrides LIMIT 0").description]
    return dict(zip(cols, r))


def caps(cfg, db=None, now: float | None = None) -> dict:
    """Effective cap per period: a live override, else the base."""
    out = base_caps(cfg)
    if db is not None:
        for p in PERIODS:
            o = override(db, p, now)
            if o:
                out[p] = o["limit_usd"]
    return out


def status(cfg, db, now: float | None = None) -> list[dict]:
    """One entry per enabled cap: period, limit (effective), base, override,
    spent, start, resets_at."""
    out = []
    now = _now(now)
    span = periods(cfg, now)
    base = base_caps(cfg)
    for period in PERIODS:
        o = override(db, period, now)
        limit = o["limit_usd"] if o else base[period]
        if limit <= 0:
            continue
        start, end = span[period]
        out.append({"period": period, "limit": limit, "base": base[period],
                    "override": o, "spent": db.spend_since(start),
                    "start": start, "resets_at": end})
    return out


def reached(cfg, db, now: float | None = None) -> dict | None:
    """The cap that blocks new turns now, or None. With both reached, the
    one that resets last (that is when turns resume)."""
    over = [s for s in status(cfg, db, now) if s["spent"] > s["limit"]]
    return max(over, key=lambda s: s["resets_at"]) if over else None


def notice_key(cap: dict) -> str:
    """budget_notices key: a raised cap reached again in the same period is
    news again."""
    return f"{cap['period']}:{int(cap['start'])}:{cap['limit']:.2f}"


def _fmt_ts(cfg, ts: float) -> str:
    local = dt.datetime.fromtimestamp(ts, _zone(cfg))
    return local.strftime("%d/%m %H:%M" if _pt(cfg) else "%a %d %b %H:%M")


def _names(cfg) -> dict:
    return ({"day": "diário", "week": "semanal"} if _pt(cfg)
            else {"day": "daily", "week": "weekly"})


def describe(cfg, cap: dict) -> dict:
    """Fill-ins for the budget_reached message."""
    return {"period": _names(cfg)[cap["period"]], "limit": f"{cap['limit']:.2f}",
            "spent": f"{cap['spent']:.2f}",
            "resets": _fmt_ts(cfg, cap["resets_at"])}


# -- changes --------------------------------------------------------------------

def cli_ceiling(cfg, period: str) -> float:
    """Highest cap the local agent CLI may set; 0 = the CLI may not raise."""
    key = "budget_cli_max_daily_usd" if period == "day" else \
        "budget_cli_max_weekly_usd"
    return float(cfg.get(key) or 0)


def _cli_may_replace(db, period: str, now: float):
    """The agent CLI never undoes the owner: a live override the owner set
    (WhatsApp command or tool) can only be changed by the owner."""
    o = override(db, period, now)
    if o and o["source"] != "cli":
        raise BudgetError(f"the current {period} cap was set by the owner "
                          f"({o['by']} via {o['source']}); only the owner "
                          f"can change it")


def set_override(cfg, db, period: str, value: str | float, *, source: str,
                 by: str, channel: str = "", reason: str = "",
                 now: float | None = None) -> dict:
    """Set the cap for the rest of the current `period`. `value` is an amount
    ("5", 5.0) or an increment on the current effective cap ("+2"). Returns
    the new override row. Raises BudgetError when refused."""
    if period not in PERIODS:
        raise BudgetError(f"unknown period {period!r} (day | week)")
    if source not in SOURCES:
        raise BudgetError(f"unknown source {source!r}")
    if not str(by).strip():
        raise BudgetError("who is changing it is required")
    now = _now(now)
    raw = re.sub(r"(?i)\s|us\$|\$", "", str(value)).replace(",", ".")
    relative = raw.startswith("+")
    try:
        amount = float(raw[1:] if relative else raw)
    except ValueError:
        raise BudgetError(f"not an amount: {value!r}")
    current = caps(cfg, db, now)[period]
    if source == "cli":
        _cli_may_replace(db, period, now)
    limit = round(current + amount if relative else amount, 2)
    if not math.isfinite(limit) or limit <= 0:
        raise BudgetError("the cap must be a positive amount")
    if relative and current <= 0:
        raise BudgetError(f"the {period} cap is disabled; give an amount")
    if source == "cli":
        ceiling = cli_ceiling(cfg, period)
        if limit > ceiling:
            raise BudgetError(
                f"the agent CLI may set the {period} cap up to US$ "
                f"{ceiling:.2f} (budget_cli_max_"
                f"{'daily' if period == 'day' else 'weekly'}_usd); asked "
                f"US$ {limit:.2f}. Only the owner can go higher.")
    start, end = periods(cfg, now)[period]
    cur = db.conn.execute(
        "INSERT INTO budget_overrides(period,period_start,limit_usd,valid_until,"
        "source,by,channel,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (period, start, limit, end, source, str(by)[:200], channel[:200],
         reason[:500], now))
    db.conn.commit()
    row = override(db, period, now)
    db.step("", "budget_override", channel=channel, period=period,
            limit=limit, previous=current, valid_until=end, source=source,
            by=str(by)[:200], reason=reason[:500], override_id=cur.lastrowid)
    return row


def cancel_override(cfg, db, period: str, *, source: str, by: str,
                    channel: str = "", now: float | None = None) -> int:
    """Back to the base cap for `period`. Returns how many live overrides
    were cancelled (0: there was none)."""
    if period not in PERIODS:
        raise BudgetError(f"unknown period {period!r} (day | week)")
    if not str(by).strip():
        raise BudgetError("who is changing it is required")
    now = _now(now)
    if source == "cli":
        _cli_may_replace(db, period, now)
    n = db.conn.execute(
        "UPDATE budget_overrides SET cancelled_at=?, cancelled_by=? WHERE "
        "period=? AND cancelled_at IS NULL AND valid_until>?",
        (now, f"{source}:{str(by)[:200]}", period, now)).rowcount
    db.conn.commit()
    if n:
        db.step("", "budget_override_cancelled", channel=channel, period=period,
                source=source, by=str(by)[:200], cancelled=n)
    return n


def history(db, limit: int = 20) -> list[dict]:
    cur = db.conn.execute("SELECT * FROM budget_overrides ORDER BY id DESC "
                          "LIMIT ?", (limit,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# -- text ------------------------------------------------------------------------

def binding(cfg, db, now: float | None = None) -> dict | None:
    """The cap with the least left: the one that will actually stop turns."""
    st = status(cfg, db, now)
    return min(st, key=lambda s: s["limit"] - s["spent"]) if st else None


def status_text(cfg, db, now: float | None = None) -> str:
    pt = _pt(cfg)
    names = _names(cfg)
    st = status(cfg, db, now)
    if not st:
        return ("Sem teto de gastos ativo." if pt
                else "No spending cap is enabled.")
    lines = ["Orçamento:" if pt else "Budget:"]
    for s in st:
        left = s["limit"] - s["spent"]
        pct = 100 * s["spent"] / s["limit"]
        o = s["override"]
        if o:
            lim = (f"US$ {s['limit']:.2f} temporário até "
                   f"{_fmt_ts(cfg, o['valid_until'])} (base {s['base']:.2f}, "
                   f"por {o['by']} via {o['source']})" if pt else
                   f"US$ {s['limit']:.2f} temporary until "
                   f"{_fmt_ts(cfg, o['valid_until'])} (base {s['base']:.2f}, "
                   f"by {o['by']} via {o['source']})")
        else:
            lim = f"US$ {s['limit']:.2f}" + (" (base)" if pt else " (base)")
        if pt:
            lines.append(f"- {names[s['period']].capitalize()}: gasto US$ "
                         f"{s['spent']:.2f} de {lim} ({pct:.0f}%), "
                         + (f"faltam US$ {left:.2f}" if left > 0 else
                            "limite atingido")
                         + f"; renova {_fmt_ts(cfg, s['resets_at'])}")
        else:
            lines.append(f"- {names[s['period']].capitalize()}: spent US$ "
                         f"{s['spent']:.2f} of {lim} ({pct:.0f}%), "
                         + (f"US$ {left:.2f} left" if left > 0 else
                            "cap reached")
                         + f"; resets {_fmt_ts(cfg, s['resets_at'])}")
    b = binding(cfg, db, now)
    if b and len(st) > 1:
        lines.append(f"Quem trava primeiro: o {names[b['period']]}." if pt
                     else f"The {names[b['period']]} cap binds first.")
    return "\n".join(lines)


def change_text(cfg, row: dict) -> str:
    names = _names(cfg)
    if _pt(cfg):
        return (f"Teto {names[row['period']]} agora US$ {row['limit_usd']:.2f} "
                f"até {_fmt_ts(cfg, row['valid_until'])} (temporário; a base "
                f"volta sozinha).")
    return (f"{names[row['period']].capitalize()} cap is now US$ "
            f"{row['limit_usd']:.2f} until {_fmt_ts(cfg, row['valid_until'])} "
            f"(temporary; the base comes back by itself).")


def cancel_text(cfg, period: str, n: int) -> str:
    names = _names(cfg)
    if _pt(cfg):
        return (f"Aumento {names[period]} cancelado; vale a base de novo."
                if n else f"Não havia aumento {names[period]} ativo.")
    return (f"{names[period].capitalize()} override cancelled; back to base."
            if n else f"There was no {names[period]} override.")


# -- alerts ---------------------------------------------------------------------

def alert_channel(cfg) -> str:
    """Where 80%/100% alerts go: [model] budget_alert_channel, else the
    owner's WhatsApp (first owner-tier contact), else none."""
    ch = str(cfg.get("budget_alert_channel") or "")
    if ch:
        return ch
    from .tools import contacts
    for alias, c in contacts(cfg).items():
        if c.get("tier") == "owner":
            return f"wpp:{alias}"
    return ""


def due_alerts(cfg, db, now: float | None = None) -> list[str]:
    """Claim the alerts due now and return their texts (at most one per cap:
    the highest threshold crossed; lower ones are claimed with it)."""
    now = _now(now)
    names = _names(cfg)
    pt = _pt(cfg)
    texts = []
    st = status(cfg, db, now)
    b = binding(cfg, db, now)
    for s in st:
        pct = 100 * s["spent"] / s["limit"]
        crossed = [t for t in THRESHOLDS if pct >= t]
        if not crossed:
            continue
        fresh = [t for t in crossed if db.conn.execute(
            "INSERT OR IGNORE INTO budget_alerts VALUES(?,?,?,?,?)",
            (s["period"], s["start"], t, round(s["limit"], 2), now)
        ).rowcount == 1]
        db.conn.commit()
        if not fresh:
            continue
        top = max(fresh)
        name = names[s["period"]]
        resets = _fmt_ts(cfg, s["resets_at"])
        if pt:
            msg = (f"⚠️ Orçamento {name}: atingi o limite de US$ "
                   f"{s['limit']:.2f} (gasto US$ {s['spent']:.2f}). Novas "
                   f"mensagens ficam guardadas até {resets}."
                   if top >= 100 else
                   f"⚠️ Orçamento {name}: {pct:.0f}% usado (US$ "
                   f"{s['spent']:.2f} de {s['limit']:.2f}; renova {resets}).")
            if b and b["period"] != s["period"] and top < 100:
                msg += (f" Atenção: quem trava antes é o {names[b['period']]}"
                        f" (faltam US$ {b['limit'] - b['spent']:.2f}).")
            msg += (" Para liberar: \"orçamento hoje N\" ou \"orçamento "
                    "semana N\"; status: \"orçamento\".")
        else:
            msg = (f"⚠️ {name.capitalize()} budget: cap of US$ "
                   f"{s['limit']:.2f} reached (spent US$ {s['spent']:.2f}). "
                   f"New messages are held until {resets}."
                   if top >= 100 else
                   f"⚠️ {name.capitalize()} budget: {pct:.0f}% used (US$ "
                   f"{s['spent']:.2f} of {s['limit']:.2f}; resets {resets}).")
            if b and b["period"] != s["period"] and top < 100:
                msg += (f" Note: the {names[b['period']]} cap binds first "
                        f"(US$ {b['limit'] - b['spent']:.2f} left).")
        db.step("", "budget_alert", period=s["period"], threshold=top,
                limit=s["limit"], spent=round(s["spent"], 4))
        texts.append(msg)
    return texts


def send_alerts(cfg, db, now: float | None = None) -> list[str]:
    """Deliver due alerts to the owner without a model call. With no alert
    channel nothing is claimed, so nothing is lost silently."""
    ch = alert_channel(cfg)
    if not ch:
        return []
    texts = due_alerts(cfg, db, now)
    from .tools import deliver
    for t in texts:
        status_ = deliver(ch, t, "budget alert", cfg, db)
        db.step("", "budget_alert_delivery", channel=ch, status=status_)
    return texts


def announce(cfg, db, text: str):
    """Tell the owner about a change made from elsewhere (the agent CLI)."""
    ch = alert_channel(cfg)
    if ch:
        from .tools import deliver
        st = deliver(ch, text, "budget change", cfg, db)
        db.step("", "budget_change_notice", channel=ch, status=st)


# -- owner commands (no model call) ----------------------------------------------

_PERIOD_WORDS = {"hoje": "day", "dia": "day", "diario": "day", "day": "day",
                 "today": "day", "daily": "day", "semana": "week",
                 "semanal": "week", "week": "week", "weekly": "week"}
_CMD = re.compile(r"^(?:orcamento|budget)(?:\s+(.*))?$")
_AMOUNT = r"(\+?\s*(?:us\$|\$)?\s*\d+(?:[.,]\d+)?)"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).rstrip(" .!?")


def parse_command(text: str) -> tuple | None:
    """("status",) | ("set", period, value) | ("cancel", period), or None
    when `text` is not exactly a budget command."""
    m = _CMD.match(_norm(text or ""))
    if not m:
        return None
    rest = (m.group(1) or "").strip()
    if rest in ("", "status", "?"):
        return ("status",)
    m2 = re.match(r"^(cancelar|cancel)\s+(\w+)$", rest)
    if m2:
        p = _PERIOD_WORDS.get(m2.group(2))
        return ("cancel", p) if p else None
    m3 = re.match(rf"^(\w+)\s+{_AMOUNT}$", rest)
    if m3:
        p = _PERIOD_WORDS.get(m3.group(1))
        if p:
            v = re.sub(r"\s|us\$|\$", "", m3.group(2)).replace(",", ".")
            return ("set", p, v)
    return None


def may_change(env: dict) -> bool:
    """Owner, speaking from the owner's own WhatsApp or the local cli. Tier
    comes from the runtime (allowlist / local door), never from the text.
    Owner-tier comms peers (trusted machines) and agents read status only."""
    ch = str(env.get("channel") or "")
    return (env.get("tier") == "owner" and env.get("kind", "message") == "message"
            and (ch.startswith("wpp:") or ch == "cli"))


def run_command(cfg, db, cmd: tuple, env: dict, source: str = "whatsapp",
                now: float | None = None) -> str:
    """Apply a parsed command for `env` (caller checked may_change)."""
    try:
        if cmd[0] == "status":
            return status_text(cfg, db, now)
        if cmd[0] == "set":
            row = set_override(cfg, db, cmd[1], cmd[2], source=source,
                               by=env.get("sender", ""),
                               channel=env.get("channel", ""), now=now)
            return change_text(cfg, row) + "\n\n" + status_text(cfg, db, now)
        n = cancel_override(cfg, db, cmd[1], source=source,
                            by=env.get("sender", ""),
                            channel=env.get("channel", ""), now=now)
        return cancel_text(cfg, cmd[1], n) + "\n\n" + status_text(cfg, db, now)
    except BudgetError as e:
        return (f"Não alterei o orçamento: {e}" if _pt(cfg)
                else f"Budget unchanged: {e}")
