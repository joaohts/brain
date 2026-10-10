"""Inbox: every inbound message is written here first, then answered by turns.

The global turn lock only decides who answers; it never decides whether
something gets answered.

- Every inbound (HTTP /turn, WhatsApp, comms, CLI, worker results, timers) is
  an `inbox` row: sender, tier, channel, thread, provider_id (UNIQUE, so
  redeliveries are dropped), received_at and state unread → read_by_turn → done.
- One turn at a time across processes: a turn holds the `turn_lease` row
  (holder + expires_at, renewed every step and by a heartbeat). An expired
  lease is reclaimable, so a crashed turn can't hold it forever. A running
  turn is never cancelled.
- Before each model step the running turn pulls unread rows from the SAME
  channel + thread + sender (worker results for that thread too) and merges
  them in. Other senders always get their own turn, at their own tier.
- At turn end the dispatcher hands off to the oldest unread row without
  releasing the lease; with nothing unread it releases, then re-checks.
- Which rows a turn read is recorded per row (turn_id), never as a newest-id
  watermark, so out-of-order commits can't skip a message.
- Worker results (messages from Claude sessions the brain spawned) become
  rows addressed to the ORIGIN thread at tier "agent", and their reply is
  delivered to the origin channel. A worker whose result was marked final
  goes idle once that result is delivered and stays reachable for
  follow-ups. Any worker, idle or not, is reaped once it has been inactive
  (no report, follow-up or pane output) for [claude_sessions] idle_minutes;
  idle_minutes = 0 reaps at [FINAL]. Sending it a follow-up wakes it before
  the send.
- Spending caps (brain/budget.py) are checked before a turn starts. While
  one is reached, the next unread row is held instead of answered: the row
  finishes (its sender gets the budget_reached notice, at most once per
  channel per cap period, otherwise an empty reply) and, in the same
  transaction, a copy is stored in state `held`, addressed to the row's
  origin channel. Once spend is back under every cap the held copies become
  unread again, oldest first, and their replies are delivered to that
  channel. A turn already running is never cut short by a cap. The notice
  is keyed by cap, period and effective limit, so a raised cap reached
  again in the same period is announced again.
- Owner budget commands ("orçamento", "orçamento hoje 5", ...; see
  brain/budget.py) are answered by the dispatcher itself, before the cap
  check and without a model call, so they work while a cap holds messages.
  A running turn never merges one; it waits for its own dispatch. After
  every turn and on every tick, the 80%/100% alerts are checked.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

LEASE_SECONDS = 180
MERGE_MAX_MSGS = 20
MERGE_MAX_CHARS = 4000
MAX_DRAFT_DISCARDS = 3
FINAL_MARK = "[FINAL]"
DEFAULT_IDLE_MINUTES = 480   # [claude_sessions] idle_minutes when unset or 0
REAP_EVERY = 60.0            # seconds between inactivity sweeps


_PORTER_WHO: dict = {"at": 0.0, "map": {}}


def _porter_who(head: dict) -> str:
    """'João · WhatsApp' / 'personal-mac:notes · comms': who a turn answers,
    with the channel to disambiguate. Comms ids resolve to addresses through
    a cached `comms who` (5 min); anything unknown falls back to the raw id."""
    import re
    import subprocess
    ch = head.get("channel") or ""
    kind, _, rest = ch.partition(":")
    if kind.startswith("comms"):
        if time.time() - _PORTER_WHO["at"] > 300:
            _PORTER_WHO["at"] = time.time()
            try:
                out = subprocess.run(
                    [os.path.expanduser("~/.local/bin/comms"), "--compact", "who"],
                    capture_output=True, text=True, timeout=3).stdout
                _PORTER_WHO["map"] = {r["recipient"]: r["address"]
                                      for r in json.loads(out or "[]")}
            except Exception:
                pass
        return f"{_PORTER_WHO['map'].get(rest, rest)} · comms"
    name = re.sub(r"\s*\([^)]*\)", "", head.get("sender") or rest).strip()
    label = {"wpp": "WhatsApp", "voice": "voice", "cli": "cli"}.get(kind, kind or "?")
    return f"{name or rest} · {label}"

def _porter(kind: str, summary: str = "", error_kind: str = "") -> None:
    """Report Joana to porter (comms) so she shows in João's Monitor app.
    Fire-and-forget: never blocks or fails a turn."""
    import datetime as _dt
    import subprocess
    args = [os.path.expanduser("~/.local/bin/comms"), "porter", "event",
            "--agent", "joana", "--harness", "joana", "--title", "Joana",
            "--project", "brain", "--kind", kind,
            "--at", _dt.datetime.now(_dt.timezone.utc).isoformat()]
    if summary:
        args += ["--summary", summary[:120]]
    if error_kind:
        args += ["--error-kind", error_kind[:60]]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id TEXT UNIQUE,
  received_at REAL NOT NULL,
  channel TEXT NOT NULL,
  thread TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL,
  tier TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'message',
  route TEXT NOT NULL DEFAULT 'http',
  meta TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'unread',
  turn_id TEXT,
  holder TEXT,
  merged INTEGER NOT NULL DEFAULT 0,
  reply TEXT,
  error TEXT,
  done_at REAL
);
CREATE INDEX IF NOT EXISTS inbox_state ON inbox(state, id);
CREATE INDEX IF NOT EXISTS inbox_thread ON inbox(channel, thread, state, id);
CREATE TABLE IF NOT EXISTS turn_lease (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  holder TEXT,
  expires_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO turn_lease(id, holder, expires_at) VALUES (1, NULL, 0);
CREATE TABLE IF NOT EXISTS workers (
  sid TEXT PRIMARY KEY,
  recipient TEXT,
  origin_channel TEXT NOT NULL,
  origin_thread TEXT NOT NULL DEFAULT '',
  origin_sender TEXT NOT NULL,
  request TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  idle_until REAL,
  pending INTEGER NOT NULL DEFAULT 0,
  last_active REAL
);
CREATE INDEX IF NOT EXISTS workers_recipient ON workers(recipient);
CREATE TABLE IF NOT EXISTS budget_notices (
  channel TEXT NOT NULL,
  period TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY (channel, period)
);
"""


def _pid_alive(holder: str | None) -> bool:
    try:
        pid = int(str(holder).split(":", 1)[0])
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


class Store:
    """SQLite persistence for the inbox, the turn lease and worker mappings.
    Its own connection (same file as the brain DB) behind one lock."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute(
                "PRAGMA table_info(workers)")}
            if "idle_until" not in cols:
                self.conn.execute("ALTER TABLE workers ADD COLUMN idle_until REAL")
            if "pending" not in cols:
                # a worker still on its task owes that task's [FINAL]
                self.conn.execute("ALTER TABLE workers ADD COLUMN pending "
                                  "INTEGER NOT NULL DEFAULT 0")
                self.conn.execute("UPDATE workers SET pending=1 WHERE "
                                  "state IN ('starting','running')")
            if "last_active" not in cols:
                # unknown until observed; the reaper reads the pane first
                self.conn.execute("ALTER TABLE workers ADD COLUMN last_active REAL")

    def _tx(self):
        store = self

        class Tx:
            def __enter__(self):
                store.lock.acquire()
                store.conn.execute("BEGIN IMMEDIATE")
                return store.conn

            def __exit__(self, exc_type, *a):
                try:
                    store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
        return Tx()

    # -- rows ------------------------------------------------------------------
    def add(self, env: dict, route: str = "http", meta: dict | None = None,
            kind: str = "message") -> tuple[int, bool]:
        """Insert an inbound row. Returns (id, duplicate)."""
        pid = env.get("provider_id") or None
        with self._tx() as c:
            if pid:
                old = c.execute("SELECT id FROM inbox WHERE provider_id=?",
                                (pid,)).fetchone()
                if old:
                    return old["id"], True
            cur = c.execute(
                "INSERT INTO inbox(provider_id,received_at,channel,thread,sender,"
                "tier,text,kind,route,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, time.time(), env["channel"], env.get("thread") or "",
                 env["sender"], env["tier"], env["text"], kind, route,
                 json.dumps(meta or {}, ensure_ascii=False)))
            return cur.lastrowid, False

    def get(self, row_id: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM inbox WHERE id=?",
                                  (row_id,)).fetchone()
        return _row(r)

    def next_unread(self, routes) -> dict | None:
        q = ",".join("?" for _ in routes)
        with self.lock:
            r = self.conn.execute(
                f"SELECT * FROM inbox WHERE state='unread' AND route IN ({q}) "
                f"ORDER BY id LIMIT 1", tuple(routes)).fetchone()
        return _row(r)

    def has_unread(self, routes) -> bool:
        return self.next_unread(routes) is not None

    def mark_read(self, ids, turn_id: str, holder: str, merged=False):
        if not ids:
            return
        with self._tx() as c:
            c.executemany(
                "UPDATE inbox SET state='read_by_turn', turn_id=?, holder=?, "
                "merged=? WHERE id=? AND state='unread'",
                [(turn_id, holder, int(merged), i) for i in ids])

    def pull(self, channel: str, thread: str, sender: str, turn_id: str,
             holder: str, max_msgs=MERGE_MAX_MSGS, max_chars=MERGE_MAX_CHARS):
        """Unread rows the running turn may merge: same channel + thread, and
        the same sender (or a worker result for this thread). Marked read by
        this turn atomically, capped per read; the rest wait for the next one."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM inbox WHERE state='unread' AND channel=? AND "
                "thread=? AND (sender=? OR kind='worker_result') ORDER BY id "
                "LIMIT ?", (channel, thread, sender, max_msgs)).fetchall()
            from .budget import parse_command
            taken, chars = [], 0
            for r in rows:
                if taken and chars + len(r["text"]) > max_chars:
                    break
                if r["kind"] == "message" and parse_command(r["text"]):
                    break   # a budget command gets its own dispatch
                taken.append(r)
                chars += len(r["text"])
            c.executemany(
                "UPDATE inbox SET state='read_by_turn', turn_id=?, holder=?, "
                "merged=1 WHERE id=?", [(turn_id, holder, r["id"]) for r in taken])
        return [_row(r) for r in taken]

    def finish(self, row_id: int, reply: str | None, error: str | None = None):
        with self._tx() as c:
            c.execute("UPDATE inbox SET state='done', reply=?, error=?, "
                      "done_at=? WHERE id=?", (reply, error, time.time(), row_id))

    def hold(self, row: dict, period: str, notice: str) -> tuple[int, str] | None:
        """Hold an unread row while a spending cap is reached: finish it and,
        atomically, store a `held` copy addressed to its origin channel.
        Returns (copy id, reply for the original: `notice` the first time
        this channel hears of this cap period, else ""), or None if the row
        was no longer unread."""
        meta = dict(row["meta"])
        origin = meta.get("deliver_to") or row["channel"]
        meta.update(deliver_to=origin, held_from=row["id"], held_at=time.time())
        with self._tx() as c:
            cur = c.execute("UPDATE inbox SET state='done', done_at=? WHERE "
                            "id=? AND state='unread'", (time.time(), row["id"]))
            if cur.rowcount != 1:
                return None
            first = c.execute("INSERT OR IGNORE INTO budget_notices(channel,"
                              "period,ts) VALUES(?,?,?)",
                              (origin, period, time.time())).rowcount == 1
            reply = notice if first else ""
            c.execute("UPDATE inbox SET reply=? WHERE id=?", (reply, row["id"]))
            copy = c.execute(
                "INSERT INTO inbox(provider_id,received_at,channel,thread,sender,"
                "tier,text,kind,route,meta,state) VALUES(NULL,?,?,?,?,?,?,?,"
                "'deliver',?,'held')",
                (row["received_at"], row["channel"], row["thread"], row["sender"],
                 row["tier"], row["text"], row["kind"],
                 json.dumps(meta, ensure_ascii=False))).lastrowid
        return copy, reply

    def has_held(self) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM inbox WHERE state='held' "
                                     "LIMIT 1").fetchone() is not None

    def release_held(self) -> int:
        """Spend is under every cap again: held rows go back to unread."""
        with self._tx() as c:
            return c.execute("UPDATE inbox SET state='unread' WHERE "
                             "state='held'").rowcount

    def read_by(self, turn_id: str) -> list[dict]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(
                "SELECT * FROM inbox WHERE turn_id=? ORDER BY id", (turn_id,))]

    # -- lease -----------------------------------------------------------------
    def claim_lease(self, holder: str, seconds: float = LEASE_SECONDS) -> bool:
        now = time.time()
        with self._tx() as c:
            cur = c.execute("SELECT holder, expires_at FROM turn_lease WHERE id=1"
                            ).fetchone()
            prev = cur["holder"]
            if prev and prev != holder and cur["expires_at"] >= now:
                return False
            c.execute("UPDATE turn_lease SET holder=?, expires_at=? WHERE id=1",
                      (holder, now + seconds))
            if prev and prev != holder and not _pid_alive(prev):
                # the previous holder crashed mid-turn: its rows go back to
                # unread so they are answered (possibly twice, never zero times)
                c.execute("UPDATE inbox SET state='unread', turn_id=NULL, "
                          "holder=NULL, merged=0 WHERE state='read_by_turn' "
                          "AND holder=?", (prev,))
            return True

    def renew_lease(self, holder: str, seconds: float = LEASE_SECONDS) -> bool:
        with self._tx() as c:
            cur = c.execute("UPDATE turn_lease SET expires_at=? WHERE id=1 AND "
                            "holder=?", (time.time() + seconds, holder))
            return cur.rowcount == 1

    def release_lease(self, holder: str):
        with self._tx() as c:
            c.execute("UPDATE turn_lease SET holder=NULL, expires_at=0 WHERE "
                      "id=1 AND holder=?", (holder,))

    def lease(self) -> dict:
        with self.lock:
            return dict(self.conn.execute(
                "SELECT holder, expires_at FROM turn_lease WHERE id=1").fetchone())

    def recover(self, own_holder_prefix: str):
        """Startup: rows read by turns of dead processes go back to unread."""
        with self._tx() as c:
            rows = c.execute("SELECT DISTINCT holder FROM inbox WHERE "
                             "state='read_by_turn'").fetchall()
            for r in rows:
                h = r["holder"]
                if h and not h.startswith(own_holder_prefix) and not _pid_alive(h):
                    c.execute("UPDATE inbox SET state='unread', turn_id=NULL, "
                              "holder=NULL, merged=0 WHERE state='read_by_turn' "
                              "AND holder=?", (h,))
            lease = c.execute("SELECT holder FROM turn_lease WHERE id=1").fetchone()
            if lease["holder"] and not _pid_alive(lease["holder"]):
                c.execute("UPDATE turn_lease SET holder=NULL, expires_at=0 "
                          "WHERE id=1")

    # -- workers (delegations) ---------------------------------------------------
    def add_worker(self, sid, origin_channel, origin_thread, origin_sender, request):
        now = time.time()
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO workers(sid,recipient,origin_channel,"
                      "origin_thread,origin_sender,request,state,created_at,"
                      "pending,last_active) VALUES(?,NULL,?,?,?,?,'starting',?,1,?)",
                      (sid, origin_channel, origin_thread or "", origin_sender,
                       request[:500], now, now))

    def set_worker(self, sid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE workers SET {cols} WHERE sid=?",
                      (*fields.values(), sid))

    def worker_by_recipient(self, recipient: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM workers WHERE recipient=? AND state<>'reaped' "
                "ORDER BY created_at DESC LIMIT 1", (recipient,)).fetchone()
        return dict(r) if r else None

    # Every live worker (starting, running or idle) is reaped once it has been
    # inactive for [claude_sessions] idle_minutes. `last_active` is the latest
    # activity seen: its spawn, each report it sent, each follow-up handed to
    # it, and output in its tmux pane (Brain.reap_idle). It only moves forward.
    #
    # `pending` counts the tasks a worker owes a [FINAL] for: its spawn task
    # plus each follow-up. It is idle only while it owes none, so a [FINAL]
    # that crossed a newer follow-up can't mark a busy worker idle (or, with
    # idle_minutes = 0, reap it at once).

    def touch_worker(self, sid: str, at: float | None = None):
        """Activity seen at `at` (default now)."""
        at = time.time() if at is None else at
        with self._tx() as c:
            c.execute("UPDATE workers SET last_active=MAX(COALESCE(last_active,"
                      "created_at),?) WHERE sid=? AND state NOT IN "
                      "('reaped','failed')", (at, sid))

    def live_workers(self) -> list[str]:
        with self.lock:
            return [r["sid"] for r in self.conn.execute(
                "SELECT sid FROM workers WHERE state NOT IN ('reaped','failed')")]

    def inactive(self, cutoff: float) -> list[str]:
        """Live workers with no activity after `cutoff`."""
        with self.lock:
            return [r["sid"] for r in self.conn.execute(
                "SELECT sid FROM workers WHERE state NOT IN ('reaped','failed') "
                "AND COALESCE(last_active,created_at)<=?", (cutoff,))]

    def take_followup(self, target: str) -> tuple[str, dict] | None:
        """A message is about to go to comms `target`. If that is a live
        worker, wake it before the send: the follow-up counts as activity,
        it leaves idle and owes one more [FINAL]. Returns (sid, previous
        fields) or None."""
        last = target.rsplit(":", 1)[-1]
        sid = last[len("session-"):] if last.startswith("session-") else None
        now = time.time()
        with self._tx() as c:
            w = c.execute(
                "SELECT * FROM workers WHERE (recipient=? OR sid=?) AND state "
                "NOT IN ('reaped','failed') ORDER BY created_at DESC LIMIT 1",
                (target, sid)).fetchone()
            if not w:
                return None
            c.execute("UPDATE workers SET pending=pending+1, idle_until=NULL, "
                      "last_active=MAX(COALESCE(last_active,created_at),?), "
                      "state=CASE state WHEN 'idle' THEN 'running' ELSE state "
                      "END WHERE sid=?", (now, w["sid"]))
            touched = c.execute("SELECT last_active FROM workers WHERE sid=?",
                                (w["sid"],)).fetchone()["last_active"]
            prev = {k: w[k] for k in ("state", "idle_until", "last_active")}
            prev["touched"] = touched
            return w["sid"], prev

    def drop_followup(self, sid: str, prev: dict):
        """The follow-up was never handed off: undo take_followup (its
        activity too, unless newer activity was seen since)."""
        with self._tx() as c:
            c.execute("UPDATE workers SET pending=MAX(pending-1,0) WHERE sid=?",
                      (sid,))
            c.execute("UPDATE workers SET last_active=? WHERE sid=? AND "
                      "last_active=?", (prev.get("last_active"), sid,
                                        prev.get("touched")))
            if prev["state"] == "idle":
                c.execute("UPDATE workers SET state='idle', idle_until=? WHERE "
                          "sid=? AND state='running' AND pending=0",
                          (prev["idle_until"], sid))

    def worker_final(self, sid: str, reap: bool = False) -> str | None:
        """A [FINAL] reached the origin: activity, and one owed task settled.
        Once none is owed the worker is idle (reap: reaped at once).
        Returns the new state, or None while it still has work."""
        now = time.time()
        with self._tx() as c:
            c.execute("UPDATE workers SET pending=MAX(pending-1,0), last_active="
                      "MAX(COALESCE(last_active,created_at),?) WHERE sid=? "
                      "AND state NOT IN ('reaped','failed')", (now, sid))
            w = c.execute("SELECT state, pending FROM workers WHERE sid=?",
                          (sid,)).fetchone()
            if not w or w["state"] in ("reaped", "failed") or w["pending"]:
                return None
            state = "reaped" if reap else "idle"
            c.execute("UPDATE workers SET state=?, idle_until=NULL WHERE sid=?",
                      (state, sid))
            return state

    def worker_progress(self, sid: str):
        """A progress report: activity, and the worker is working, whatever
        we knew."""
        self.touch_worker(sid)
        with self._tx() as c:
            c.execute("UPDATE workers SET state='running', idle_until=NULL, "
                      "pending=MAX(pending,1) WHERE sid=? AND state='idle'",
                      (sid,))

    def claim_reap(self, sid: str, cutoff: float | None = None) -> bool:
        """Mark the worker reaped; with `cutoff`, only if it is still live and
        inactive since then (a follow-up or report may have landed since it
        was listed)."""
        with self._tx() as c:
            if cutoff is None:
                cur = c.execute("UPDATE workers SET state='reaped', idle_until="
                                "NULL, pending=0 WHERE sid=? AND state<>'reaped'",
                                (sid,))
            else:
                cur = c.execute("UPDATE workers SET state='reaped', idle_until="
                                "NULL, pending=0 WHERE sid=? AND state NOT IN "
                                "('reaped','failed') AND COALESCE(last_active,"
                                "created_at)<=?", (sid, cutoff))
            return cur.rowcount == 1

    def worker(self, sid: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM workers WHERE sid=?",
                                  (sid,)).fetchone()
        return dict(r) if r else None


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def worker_result_envelope(worker: dict, sender_label: str, body: str,
                           provider_id: str) -> tuple[dict, dict]:
    """(envelope, meta) for a worker's message: addressed to the ORIGIN thread,
    tier agent, its reply delivered to the origin channel."""
    final = body.lstrip().startswith(FINAL_MARK)
    text = (f"[worker report, {'final' if final else 'progress'}, session "
            f"{worker['sid']}]\n{body}")
    env = {"channel": worker["origin_channel"],
           "thread": worker.get("origin_thread") or "",
           "sender": sender_label, "tier": "agent", "text": text,
           "provider_id": provider_id}
    meta = {"deliver_to": worker["origin_channel"], "worker_sid": worker["sid"],
            "final": final, "request": worker["request"][:200]}
    return env, meta


def stamp(row: dict) -> str:
    """User-message text for a merged row: its own envelope, visibly."""
    return (f"[{row['sender']} · tier {row['tier']} · {row['channel']}"
            f"{' · ' + row['thread'] if row.get('thread') else ''}] {row['text']}")


_current: "Brain | None" = None
_current_guard = threading.RLock()


def current() -> "Brain | None":
    return _current


def get(cfg, db, client_factory=None) -> "Brain":
    """Process-wide Brain for this config (server, CLI, comms all share it)."""
    global _current
    with _current_guard:
        if _current is None or _current.cfg["db_path"] != cfg["db_path"]:
            _current = Brain(cfg, db, client_factory=client_factory)
        elif client_factory is not None:
            _current.client_factory = client_factory
        return _current


class Brain:
    """Dispatcher: owns the lease while turns run, hands off between turns."""

    def __init__(self, cfg, db, client_factory=None, routes=None,
                 lease_seconds: float = LEASE_SECONDS):
        global _current
        self.cfg, self.db = cfg, db
        self.store = Store(cfg["db_path"])
        self.prefix = f"{os.getpid()}:"
        self.holder = f"{self.prefix}{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.client_factory = client_factory
        self._client = None
        self._local = threading.Lock()
        self._changed = threading.Condition()
        # route name -> fn(row, reply_or_None_on_error). "http" callers poll.
        self.routes = {"http": None, "deliver": None}
        self.routes.update(routes or {})
        self._ticker = None
        self._stop = threading.Event()
        with _current_guard:
            _current = self

    # -- lifecycle ---------------------------------------------------------------
    def start(self, tick: float = 2.0):
        """Recover rows of crashed turns and keep a slow ticker that picks up
        rows written by other processes (e.g. the CLI) or left behind."""
        self.store.recover(self.prefix)
        self.resume_held()
        if self._ticker is None:
            def loop():
                next_reap = 0.0
                while not self._stop.wait(tick):
                    try:
                        self.resume_held()
                        self.budget_alerts()
                    except Exception as e:
                        self.db.step("", "budget_error",
                                     error=f"{type(e).__name__}: {e}")
                    if self.store.has_unread(self.routes):
                        self.kick()
                    if time.monotonic() >= next_reap:
                        next_reap = time.monotonic() + REAP_EVERY
                        try:
                            self.reap_idle()
                        except Exception as e:
                            self.db.step("", "reap_error",
                                         error=f"{type(e).__name__}: {e}")
            self._ticker = threading.Thread(target=loop, daemon=True,
                                            name="inbox-ticker")
            self._ticker.start()
        self.kick()
        return self

    def stop(self):
        self._stop.set()

    def client(self):
        if self._client is None:
            if self.client_factory:
                self._client = self.client_factory()
            else:
                from openai import OpenAI
                from .config import api_key
                # generous per-call ceiling: the model may legitimately think
                # for minutes on a step; this only catches hung connections
                self._client = OpenAI(api_key=api_key(), timeout=600.0,
                                      max_retries=1)
        return self._client

    # -- intake ------------------------------------------------------------------
    def submit(self, env: dict, route: str = "http", meta: dict | None = None,
               kind: str = "message", kick: bool = True) -> tuple[int, bool]:
        row_id, dup = self.store.add(env, route=route, meta=meta, kind=kind)
        if dup:
            self.db.step("", "inbox_duplicate", channel=env.get("channel", ""),
                         provider_id=env.get("provider_id"), inbox_id=row_id)
        elif kick:
            self.kick()
        return row_id, dup

    def wait(self, row_id: int, timeout: float | None = None) -> dict:
        deadline = None if timeout is None else time.time() + timeout
        while True:
            row = self.store.get(row_id)
            if row is None or row["state"] == "done":
                return row
            left = None if deadline is None else deadline - time.time()
            if left is not None and left <= 0:
                return row
            with self._changed:
                self._changed.wait(0.5 if left is None else min(0.5, left))

    def run_sync(self, env: dict, timeout: float | None = None) -> str:
        row_id, _ = self.submit(env)
        row = self.wait(row_id, timeout)
        if row and row.get("error"):
            raise RuntimeError(row["error"])
        return (row or {}).get("reply") or ""

    # -- dispatch ----------------------------------------------------------------
    def kick(self):
        if self._local.acquire(blocking=False):
            threading.Thread(target=self._dispatch, daemon=True,
                             name="inbox-dispatch").start()

    def drain(self):
        """Run the dispatcher in the calling thread (tests, one-shot CLI)."""
        self._local.acquire()
        self._dispatch()

    def _dispatch(self):
        released_cleanly = False
        hb_stop = threading.Event()
        try:
            if not self.store.claim_lease(self.holder, self.lease_seconds):
                return   # another process holds it; the ticker retries

            def heartbeat():
                while not hb_stop.wait(max(1.0, self.lease_seconds / 6)):
                    self.store.renew_lease(self.holder, self.lease_seconds)
            threading.Thread(target=heartbeat, daemon=True).start()

            while True:
                row = self.store.next_unread(self.routes)
                if row is None:
                    self.store.release_lease(self.holder)
                    row = self.store.next_unread(self.routes)   # close the race
                    if row is None:
                        released_cleanly = True
                        return
                    if not self.store.claim_lease(self.holder, self.lease_seconds):
                        return
                if self._command(row):
                    continue
                from . import budget
                cap = budget.reached(self.cfg, self.db)
                if cap:
                    self._hold(row, cap)
                    continue
                self._turn(row)
        finally:
            hb_stop.set()
            if not released_cleanly:
                self.store.release_lease(self.holder)
            self._local.release()
            with self._changed:
                self._changed.notify_all()
            # a submit may have lost the race for _local after our last check
            if released_cleanly and self.store.has_unread(self.routes):
                self.kick()

    def resume_held(self) -> int:
        """Held rows go back to unread once spend is under every cap."""
        from . import budget
        if not self.store.has_held() or budget.reached(self.cfg, self.db):
            return 0
        n = self.store.release_held()
        if n:
            self.db.step("", "budget_resumed", rows=n)
            self.kick()
        return n

    def budget_alerts(self):
        """80%/100% alerts to the owner, plain text, no model call."""
        from . import budget
        budget.send_alerts(self.cfg, self.db)

    def _command(self, row: dict) -> bool:
        """An owner budget command: apply it and answer the row directly
        (brain/budget.py). False when `row` is not one this sender may run;
        it then goes the normal way (a turn, or held at a cap)."""
        from . import budget
        cmd = budget.parse_command(row["text"]) if row["kind"] == "message" \
            else None
        env = {"channel": row["channel"], "sender": row["sender"],
               "tier": row["tier"], "kind": row["kind"]}
        if not cmd or not budget.may_change(env):
            return False
        turn_id = f"t_{uuid.uuid4().hex[:10]}"
        self.store.mark_read([row["id"]], turn_id, self.holder)
        try:
            reply, error = budget.run_command(self.cfg, self.db, cmd, env), None
        except Exception as e:
            reply, error = None, f"{type(e).__name__}: {e}"
        self.db.step(turn_id, "budget_command", channel=row["channel"],
                     sender=row["sender"], command=list(cmd), error=error)
        # the thread keeps it, so later turns know what was decided
        self.db.add_message(row["channel"], "user", row["sender"], row["text"],
                            row["tier"])
        if reply:
            self.db.add_message(row["channel"], "assistant",
                                self.cfg["assistant_name"], reply)
        self._complete(row, reply, error, turn_id)
        with self._changed:
            self._changed.notify_all()
        if cmd[0] != "status":
            self.resume_held()
            self.budget_alerts()
        return True

    def _hold(self, row: dict, cap: dict):
        """A spending cap is reached: keep `row` for later (Store.hold) and
        tell its sender once per channel per cap period."""
        from . import budget
        from .config import message
        period = budget.notice_key(cap)
        notice = message(self.cfg, "budget_reached",
                         **budget.describe(self.cfg, cap))
        held = self.store.hold(row, period, notice)
        if held is None:
            return
        copy_id, reply = held
        self.db.step("", "budget_held", channel=row["channel"], inbox_id=row["id"],
                     held_id=copy_id, period=period, spent=round(cap["spent"], 4),
                     limit=cap["limit"], notified=bool(reply))
        meta = row["meta"]
        if reply and meta.get("deliver_to"):
            # an origin-addressed row (worker result): its route reaches the
            # worker, not the person, so the notice goes to the origin
            from .tools import deliver
            status = deliver(meta["deliver_to"], reply, "budget notice",
                             self.cfg, self.db, record=False)
            self.db.step("", "origin_delivery", channel=meta["deliver_to"],
                         status=status)
            reply = ""
        fn = self.routes.get(row["route"])
        if fn:
            try:
                fn(row, reply)
            except Exception as e:
                self.db.step("", "route_error", channel=row["channel"],
                             route=row["route"], error=f"{type(e).__name__}: {e}")
        with self._changed:
            self._changed.notify_all()

    def _turn(self, head: dict):
        from .loop import _run
        turn_id = f"t_{uuid.uuid4().hex[:10]}"
        self.store.mark_read([head["id"]], turn_id, self.holder)
        text = head["text"]
        if head["meta"].get("held_from"):
            import datetime as _dt
            from .budget import _zone
            at = _dt.datetime.fromtimestamp(head["received_at"],
                                            _zone(self.cfg))
            text += (f"\n[held by the spending limit: received "
                     f"{at:%Y-%m-%d %H:%M}, answered now that it reset]")
        env = {"channel": head["channel"], "thread": head["thread"],
               "sender": head["sender"], "tier": head["tier"],
               "text": text, "kind": head["kind"],
               "meta": head["meta"],
               "origin": head["meta"].get("deliver_to") or head["channel"]}

        def pull():
            rows = self.store.pull(head["channel"], head["thread"],
                                   head["sender"], turn_id, self.holder)
            return rows

        def renew():
            self.store.renew_lease(self.holder, self.lease_seconds)

        reply, error = None, None
        who = _porter_who(head)
        _porter("prompt", f"Answering {who}")
        try:
            reply = _run(env, self.cfg, self.db, self.client(), pull=pull,
                         renew=renew, turn_id=turn_id)
        except Exception as e:   # the turn failed; its rows are still answered
            error = f"{type(e).__name__}: {e}"
            self.db.step(turn_id, "turn_error", channel=head["channel"],
                         error=error)
        if error:
            _porter("error", f"Failed answering {who}", error_kind=error.split(":")[0])
        else:
            _porter("stop", f"Last answered {who}")
        for row in self.store.read_by(turn_id):
            is_head = row["id"] == head["id"]
            self._complete(row, reply if is_head else "", error, turn_id)
        with self._changed:
            self._changed.notify_all()
        try:
            self.budget_alerts()
        except Exception as e:
            self.db.step(turn_id, "budget_error", error=f"{type(e).__name__}: {e}")

    def _complete(self, row: dict, reply: str | None, error: str | None,
                  turn_id: str):
        """Persist the outcome, then hand the reply to the row's route.
        A reply for an origin-addressed row (worker result, spawn failure)
        goes to the origin channel; the row's own route then gets ""."""
        meta = row["meta"]
        delivered = False
        route_reply = reply
        if meta.get("deliver_to") and reply is not None:
            if reply.strip() and "NO_REPLY" not in reply[:40]:
                from .tools import deliver
                status = deliver(meta["deliver_to"], reply,
                                 f"result for {meta['deliver_to']}",
                                 self.cfg, self.db, record=False)
                delivered = not status.startswith(("unknown", "WhatsApp unavailable",
                                                   "comms-v1 unavailable",
                                                   "comms-v1 is not"))
                self.db.step(turn_id, "origin_delivery", channel=meta["deliver_to"],
                             status=status)
            route_reply = ""
        self.store.finish(row["id"], reply, error)
        fn = self.routes.get(row["route"])
        if fn:
            try:
                fn(row, None if error else (route_reply or ""))
            except Exception as e:
                self.db.step(turn_id, "route_error", channel=row["channel"],
                             route=row["route"], error=f"{type(e).__name__}: {e}")
        # a final worker result was handed to its origin (directly, or merged
        # into the origin's own turn whose reply carries it): once the worker
        # owes no other [FINAL] it goes idle and stays reachable for
        # follow-ups until it has been inactive for idle_minutes. Handing it
        # a follow-up (tools.deliver) wakes it before the send; so does a
        # progress report. Every report counts as activity.
        if row["kind"] == "worker_result" and not error:
            if meta.get("final") and (delivered or row["merged"]):
                self.park(meta.get("worker_sid"), turn_id)
            elif not meta.get("final"):
                self.wake(meta.get("worker_sid"))

    def idle_seconds(self) -> float:
        """How long a worker may stay inactive before it is reaped. 0 (reap
        at [FINAL]) still leaves the default for workers that never send one."""
        minutes = float(self.cfg.get("claude_sessions", {})
                        .get("idle_minutes", 0) or 0)
        return 60 * (minutes if minutes > 0 else DEFAULT_IDLE_MINUTES)

    def reap_at_final(self) -> bool:
        return not float(self.cfg.get("claude_sessions", {})
                         .get("idle_minutes", 0) or 0) > 0

    def park(self, sid: str | None, turn_id: str = ""):
        if not sid:
            return
        w = self.store.worker(sid)
        state = self.store.worker_final(sid, reap=self.reap_at_final())
        if state == "idle":
            self.db.step(turn_id, "worker_idle", channel=w["origin_channel"],
                         session=sid, idle_seconds=self.idle_seconds())
        elif state == "reaped":
            self._killed(w, turn_id)

    def wake(self, sid: str | None):
        if sid:
            self.store.worker_progress(sid)

    # where activity.sessions looks; tests point them at fakes
    proc_root = "/proc"
    claude_home = None

    def reap_scope(self) -> str:
        return str(self.cfg.get("claude_sessions", {}).get("reap_scope")
                   or "brain")

    def in_scope(self, s, scope: str) -> str | None:
        """Why session `s` is ours to time out, or None."""
        from . import tools
        if s.tmux and s.tmux.startswith(tools.own_prefix(self.cfg)):
            return "spawned"
        if scope == "brain":
            return None
        source = tools.managed_source(s.tmux) if s.tmux else None
        if source:
            return f"managed by {source}"
        return "claude session" if scope == "all" else None

    def reap_idle(self, now: float | None = None):
        """End every Claude session in scope ([claude_sessions] reap_scope:
        brain = the ones we spawned, managed = any claude-sessions.sh one,
        all = every claude process on the host) that has been inactive for
        idle_minutes, with no tool in flight. Activity is what Claude's hooks
        report plus its transcript writes (brain/activity.py); for workers
        also each report and follow-up the brain saw. A tmux session that
        claude-sessions.sh manages is torn down; any other claude process
        gets SIGTERM (SIGKILL a sweep later if it is still there), leaving
        its terminal alone."""
        from . import activity, tools
        now = time.time() if now is None else now
        cutoff = now - self.idle_seconds()
        scope = self.reap_scope()
        found = activity.sessions(self.cfg, now, proc=self.proc_root,
                                  claude_home=self.claude_home)
        by_tmux = {s.tmux: s for s in found if s.tmux}
        for sid in self.store.live_workers():
            if sid in by_tmux:
                self.store.touch_worker(sid, by_tmux[sid].last)
        for s in found:
            why = self.in_scope(s, scope)
            if not why or s.last > cutoff or s.busy(now):
                continue
            w = self.store.worker(s.tmux) if s.tmux else None
            live_worker = w and w["state"] not in ("reaped", "failed")
            if live_worker and w["last_active"] and w["last_active"] > cutoff:
                continue   # a follow-up or report the hooks haven't seen yet
            if not activity.still_idle(s, self.cfg, cutoff, now,
                                       proc=self.proc_root,
                                       claude_home=self.claude_home):
                continue
            if live_worker and not self.store.claim_reap(s.tmux, cutoff):
                continue
            self._end(s, why, now)
        self._reap_claudeless(set(by_tmux), scope, now)

    def _reap_claudeless(self, with_claude: set, scope: str, now: float):
        """Managed tmux sessions whose claude has exited are ended
        idle_minutes after the reaper first saw them so; a live worker whose
        tmux session is gone altogether is just marked reaped."""
        from . import activity, tools
        names = tools.tmux_sessions()
        if names is None:
            return
        for sid in self.store.live_workers():
            w = self.store.worker(sid)
            if (sid not in names and now - w["created_at"] > 600
                    and self.store.claim_reap(sid)):
                self.db.step("", "worker_reaped", channel=w["origin_channel"],
                             session=sid, reason="tmux session gone")
        marks = os.path.join(activity.state_dir(self.cfg), "legacy")
        os.makedirs(marks, exist_ok=True)
        own = tools.own_prefix(self.cfg)
        for name in names:
            mark = os.path.join(marks, f"noclaude-{name}")
            managed = (name.startswith(own) or scope != "brain") and \
                tools.managed_source(name)
            if name in with_claude or not managed:
                activity._remove(mark)
                continue
            if not os.path.exists(mark):
                with open(mark, "w") as f:
                    f.write(str(now))
                continue
            try:
                with open(mark) as f:
                    first = float(f.read().strip() or now)
            except (OSError, ValueError):
                continue
            if now - first >= self.idle_seconds():
                activity._remove(mark)
                if self.store.worker(name):
                    self.store.claim_reap(name)
                self.db.step("", "worker_reaped", channel="", session=name,
                             reason="claude exited, tmux left idle")
                threading.Thread(target=tools.kill_session,
                                 args=(self.cfg, name), daemon=True).start()
        for mark in os.listdir(marks):
            if mark.startswith("noclaude-") and mark[9:] not in names:
                activity._remove(os.path.join(marks, mark))

    def _end(self, s, why: str, now: float):
        from . import tools
        reason = (f"inactive {int(now - s.last) // 60} min, {why}"
                  f"{', legacy' if s.legacy else ''}")
        if s.tmux and tools.managed_source(s.tmux):
            self.db.step("", "worker_reaped", channel="", session=s.tmux,
                         pid=s.pid, reason=reason)
            threading.Thread(target=tools.kill_session,
                             args=(self.cfg, s.tmux), daemon=True).start()
            return
        key = (s.pid, s.start)
        termed = getattr(self, "_termed", {})
        self._termed = termed
        sig = 9 if key in termed and now - termed[key] >= 30 else 15
        termed.setdefault(key, now)
        self.db.step("", "worker_reaped", channel="", session=s.tmux or "",
                     pid=s.pid, signal=sig, reason=reason)
        try:
            os.kill(s.pid, sig)
        except OSError:
            pass

    def reap(self, sid: str | None, turn_id: str = ""):
        if sid and self.store.claim_reap(sid):
            self._killed(self.store.worker(sid), turn_id)

    def _killed(self, w: dict, turn_id: str, **extra):
        self.db.step(turn_id, "worker_reaped", channel=w["origin_channel"],
                     session=w["sid"], **extra)
        from . import tools
        threading.Thread(target=tools.kill_session, args=(self.cfg, w["sid"]),
                         daemon=True).start()
