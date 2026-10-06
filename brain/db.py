"""SQLite layer: traces (steps), thread histories (messages), timers.

Machine-queried state lives here; human-read knowledge lives in markdown
(identity_file, memory_dir).
Single writer by design — the turn loop is globally serialized."""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
  ts REAL, turn_id TEXT, step TEXT, channel TEXT, model TEXT,
  ms INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_turn ON steps(turn_id);
CREATE INDEX IF NOT EXISTS idx_steps_ts ON steps(ts);

CREATE TABLE IF NOT EXISTS messages (
  ts REAL, channel TEXT, role TEXT, sender TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_chan ON messages(channel, ts);

CREATE TABLE IF NOT EXISTS timers (
  id INTEGER PRIMARY KEY, fire_ts REAL, channel TEXT, message TEXT, done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS summaries (
  channel TEXT PRIMARY KEY, ts REAL, text TEXT
);
"""


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "tier" not in cols:   # older databases
            self.conn.execute("ALTER TABLE messages ADD COLUMN tier TEXT DEFAULT ''")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(timers)")}
        for col in ("tier", "sender"):   # who scheduled it; '' on older rows
            if col not in cols:
                self.conn.execute(f"ALTER TABLE timers ADD COLUMN {col} TEXT DEFAULT ''")

    # -- traces ------------------------------------------------------------
    def step(self, turn_id: str, step: str, channel: str = "", model: str = "",
             ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
             cost_usd: float = 0.0, **payload):
        self.conn.execute(
            "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), turn_id, step, channel, model, ms,
             tokens_in, tokens_out, cost_usd,
             json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    def spend_since(self, start_ts: float) -> float:
        """Traced spend from start_ts on (brain/budget.py picks the start)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM steps WHERE ts>=?",
            (start_ts,)).fetchone()
        return row[0]

    # -- threads -----------------------------------------------------------
    def add_message(self, channel: str, role: str, sender: str, text: str,
                    tier: str = ""):
        self.conn.execute(
            "INSERT INTO messages (ts, channel, role, sender, text, tier) "
            "VALUES (?,?,?,?,?,?)", (time.time(), channel, role, sender, text, tier))
        self.conn.commit()

    def window(self, channel: str, n: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, role, sender, text FROM messages WHERE channel=? "
            "ORDER BY ts DESC LIMIT ?", (channel, n)).fetchall()
        return [dict(ts=r[0], role=r[1], sender=r[2], text=r[3])
                for r in reversed(rows)]

    def blackboard(self, exclude_channel: str, hours: float, max_lines: int) -> list[str]:
        """One line per active thread — the last message verbatim, whoever
        sent it: the point is the assistant KNOWS every conversation exists and
        where it stands; read_thread is how it digs into any of them."""
        cutoff = time.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT channel, MAX(ts), sender, text FROM messages "
            "WHERE ts>? AND channel<>? AND role<>'tool' GROUP BY channel "
            "ORDER BY MAX(ts) DESC LIMIT ?",
            (cutoff, exclude_channel, max_lines)).fetchall()
        out = []
        now = time.time()
        for chan, ts, sender, text in rows:
            mins = int((now - ts) / 60)
            head = text.replace("\n", " ")[:70]
            out.append(f"{chan} — {mins}min — {sender}: {head}")
        return out

    def tool_steps(self, turn_id: str) -> list[dict]:
        """Full tool records (name, input, output) of one turn, for tool_log."""
        import json as _json
        rows = self.conn.execute(
            "SELECT ts, payload FROM steps WHERE turn_id=? AND step='tool' "
            "ORDER BY ts", (turn_id,)).fetchall()
        out = []
        for ts, payload in rows:
            try:
                out.append(dict(ts=ts, **_json.loads(payload)))
            except Exception:
                out.append(dict(ts=ts, payload=payload))
        return out

    # -- timers ------------------------------------------------------------
    def add_timer(self, fire_ts: float, channel: str, message: str,
                  tier: str = "", sender: str = ""):
        self.conn.execute(
            "INSERT INTO timers (fire_ts, channel, message, tier, sender) "
            "VALUES (?,?,?,?,?)", (fire_ts, channel, message, tier, sender))
        self.conn.commit()

    def due_timers(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, channel, message, tier, sender FROM timers "
            "WHERE done=0 AND fire_ts<=?", (time.time(),)).fetchall()
        return [dict(id=r[0], channel=r[1], message=r[2], tier=r[3] or "",
                     sender=r[4] or "") for r in rows]

    def finish_timer(self, timer_id: int):
        self.conn.execute("UPDATE timers SET done=1 WHERE id=?", (timer_id,))
        self.conn.commit()

    # -- summaries & compaction ---------------------------------------------
    def get_summary(self, channel: str) -> str:
        row = self.conn.execute(
            "SELECT text FROM summaries WHERE channel=?", (channel,)).fetchone()
        return row[0] if row else ""

    def set_summary(self, channel: str, text: str):
        self.conn.execute(
            "INSERT INTO summaries VALUES (?,?,?) ON CONFLICT(channel) "
            "DO UPDATE SET ts=excluded.ts, text=excluded.text",
            (channel, time.time(), text))
        self.conn.commit()

    def channels_with_old(self, cutoff_ts: float) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT channel FROM messages WHERE ts<?", (cutoff_ts,))]

    def old_messages(self, channel: str, cutoff_ts: float) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, role, sender, text, tier FROM messages WHERE channel=? "
            "AND ts<? ORDER BY ts", (channel, cutoff_ts)).fetchall()
        return [dict(ts=r[0], role=r[1], sender=r[2], text=r[3], tier=r[4] or "")
                for r in rows]

    def delete_old(self, channel: str, cutoff_ts: float):
        self.conn.execute("DELETE FROM messages WHERE channel=? AND ts<?",
                          (channel, cutoff_ts))
        self.conn.commit()
