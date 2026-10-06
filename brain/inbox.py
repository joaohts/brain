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
  delivered to the origin channel. A worker whose result was marked final is
  reaped once that result is delivered.
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
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workers_recipient ON workers(recipient);
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
            taken, chars = [], 0
            for r in rows:
                if taken and chars + len(r["text"]) > max_chars:
                    break
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
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO workers(sid,recipient,origin_channel,"
                      "origin_thread,origin_sender,request,state,created_at) "
                      "VALUES(?,NULL,?,?,?,?,'starting',?)",
                      (sid, origin_channel, origin_thread or "", origin_sender,
                       request[:500], time.time()))

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
        if self._ticker is None:
            def loop():
                while not self._stop.wait(tick):
                    if self.store.has_unread(self.routes):
                        self.kick()
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

    def _turn(self, head: dict):
        from .loop import _run
        turn_id = f"t_{uuid.uuid4().hex[:10]}"
        self.store.mark_read([head["id"]], turn_id, self.holder)
        env = {"channel": head["channel"], "thread": head["thread"],
               "sender": head["sender"], "tier": head["tier"],
               "text": head["text"], "kind": head["kind"],
               "meta": head["meta"],
               "origin": head["meta"].get("deliver_to") or head["channel"]}

        def pull():
            rows = self.store.pull(head["channel"], head["thread"],
                                   head["sender"], turn_id, self.holder)
            return rows

        def renew():
            self.store.renew_lease(self.holder, self.lease_seconds)

        reply, error = None, None
        try:
            reply = _run(env, self.cfg, self.db, self.client(), pull=pull,
                         renew=renew, turn_id=turn_id)
        except Exception as e:   # the turn failed; its rows are still answered
            error = f"{type(e).__name__}: {e}"
            self.db.step(turn_id, "turn_error", channel=head["channel"],
                         error=error)
        for row in self.store.read_by(turn_id):
            is_head = row["id"] == head["id"]
            self._complete(row, reply if is_head else "", error, turn_id)
        with self._changed:
            self._changed.notify_all()

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
        # into the origin's own turn whose reply carries it): reap the worker
        if (row["kind"] == "worker_result" and meta.get("final") and not error
                and (delivered or row["merged"])):
            self.reap(meta.get("worker_sid"), turn_id)

    def reap(self, sid: str | None, turn_id: str = ""):
        if not sid:
            return
        w = self.store.worker(sid)
        if not w or w["state"] == "reaped":
            return
        self.store.set_worker(sid, state="reaped")
        self.db.step(turn_id, "worker_reaped", channel=w["origin_channel"],
                     session=sid)
        from .tools import kill_session
        threading.Thread(target=kill_session, args=(self.cfg, sid),
                         daemon=True).start()
