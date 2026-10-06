"""Tool registry with ring-based, envelope-scoped exposure.

Security model (design doc): a sender below a tool's ring never sees the tool
in the schema (un-askable beats denied), and the executor re-validates anyway.
Identity is stamped by the runtime — the model never writes the sender field."""

import datetime
import os
import shutil
import subprocess
import threading
import time
import uuid

from .config import enabled

RING = {"unknown": 0, "family": 1, "owner": 2}
# "agent" (worker results, never owner) ranks 0 and is further limited to the
# tools flagged agent=True; its send_to only reaches its origin channel.
AGENT = "agent"

CLAUDE_BIN = (shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
JOB_TIMEOUT = 600  # s


def contacts(cfg) -> dict:
    """Contact book: alias -> {name, tier, number}, read from the WhatsApp
    contacts_file. The single home for real numbers; the model only ever sees
    aliases. Empty when WhatsApp is disabled."""
    import json as _json
    if not enabled(cfg, "whatsapp"):
        return {}
    path = cfg["whatsapp"]["contacts_file"]
    try:
        raw = _json.load(open(path))
    except Exception:
        return {}
    return {v["alias"]: v for v in raw.values() if v.get("alias")}


def deliver(channel: str, message: str, origin: str, cfg, db,
            record: bool = True) -> str:
    """Route a message into a channel via its adapter (shared by send_to and
    async job delivery). record=False when the caller already stored the text
    in the thread (e.g. a turn reply delivered to its origin)."""
    if channel.startswith("wpp:") and enabled(cfg, "whatsapp"):
        import json as _json
        import urllib.request
        alias = channel.split(":", 1)[1]
        book = contacts(cfg)
        if alias not in book:   # allowlist gate: aliases only, never raw numbers
            known = " | ".join(f"wpp:{a}" for a in book)
            return f"unknown contact '{alias}'. Known channels: {known}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg['whatsapp']['port']}/send",
            data=_json.dumps({"to": alias, "text": message}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
            status = "delivered on WhatsApp"
        except Exception as e:
            status = f"WhatsApp unavailable: {e}"
        channel = f"wpp:{alias}"
    elif channel.startswith(("comms-v1:", "comms:")) and enabled(cfg, "comms"):
        from .comms_v1 import deliver as deliver_v1
        target = channel.split(":", 1)[1]
        stamped = (f"{message}\n[{origin}; replies return through "
                   f"{cfg['comms']['alias']} to that channel]"
                   if origin.startswith("relayed from ") else message)
        # a message to a worker is a follow-up: wake it before the send so
        # the idle reaper can't end it while it works on this
        store = _worker_store(cfg)
        taken = store.take_followup(target) if store else None
        result = deliver_v1(target, stamped)
        if not result["ok"]:
            if taken:
                store.drop_followup(*taken)
            return result["error"]
        status = (f"sent on comms-v1 to {target}: id {result['id']}, "
                  f"state {result['state']}")
        if taken:
            status += f"; worker session-{taken[0]} is awake until its next [FINAL]"
    elif channel == "cli":
        status = "delivered on cli"
    else:
        return f"unknown or disabled channel: {channel}"
    if record:
        db.add_message(channel, "assistant",
                       f"{cfg['assistant_name']} ({origin})", message)
    return status


def _worker_store(cfg):
    """The worker table: the running dispatcher's, else (another process,
    e.g. the CLI) the same database file."""
    from . import inbox
    brain = inbox.current()
    if brain:
        return brain.store
    try:
        return inbox.Store(cfg["db_path"])
    except Exception:
        return None


def _memory_path(cfg):
    os.makedirs(cfg["memory_dir"], exist_ok=True)
    return os.path.join(cfg["memory_dir"], "inbox.md")


# -- tool implementations: fn(envelope, args, cfg, db) -> str -----------------

def t_remember(env, args, cfg, db):
    line = f"- {datetime.date.today()} [{env['sender']} via {env['channel']}]: {args['fact']}\n"
    with open(_memory_path(cfg), "a") as f:
        f.write(line)
    return "saved to memory"


def send_origin(env, target: str) -> str:
    """Provenance for an outbound message. Only a message carried from one
    conversation into another is a relay; anything the brain says to the
    channel it is talking on is its own."""
    if env["channel"] == target:
        return "direct"
    return f"relayed from {env['channel']}"


def t_send_to(env, args, cfg, db):
    if env.get("tier") == AGENT:
        origin = env.get("origin") or env["channel"]
        if args["channel"] != origin:
            db.step(env.get("turn_id", ""), "policy_denial", channel=env["channel"],
                    tool="send_to", target=args["channel"], sender=env["sender"])
            return (f"denied by policy: an agent-tier turn can only deliver to "
                    f"its origin channel ({origin})")
    elif (args["channel"].startswith(("comms-v1:", "comms:"))
          and env.get("tier") != "owner"):
        db.step(env.get("turn_id", ""), "policy_denial", channel=env["channel"],
                tool="send_to", target=args["channel"], sender=env["sender"])
        return "denied by policy: only the owner can have me message agents"
    return deliver(args["channel"], args["message"],
                   send_origin(env, args["channel"]), cfg, db)


def _spawn_failed(reason: str) -> str:
    return (f"spawn FAILED: {reason}. Tell the user it failed; do not promise "
            f"a follow-up.")


def _pane_problem(sid: str, settled: bool) -> str:
    """Fail fast instead of polling a session that can never come up."""
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", sid],
                       capture_output=True, text=True, timeout=10)
    if r.returncode:
        return "tmux session is gone"
    pane = r.stdout
    if "Login expired" in pane or "Please run /login" in pane:
        return "Claude Code login expired on this host"
    cmd = subprocess.run(["tmux", "list-panes", "-t", sid, "-F",
                          "#{pane_current_command}"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    # claude-sessions.sh create already waited for the REPL, so a bare shell a
    # few seconds later means claude exited.
    if settled and cmd in ("bash", "sh", "zsh", "fish"):
        return "the claude process exited (harness is dead)"
    return ""


def _session_env():
    env2 = dict(os.environ)
    env2["PATH"] = (os.path.expanduser("~/.local/bin") + ":"
                    + os.path.expanduser("~/.local/node/current/bin") + ":"
                    + env2.get("PATH", "/usr/bin:/bin"))
    return env2


def _create_session(cfg, cwd: str) -> str:
    """Start a managed session; returns its id or raises with the reason."""
    import json as _json
    r = subprocess.run(["bash", cfg["claude_sessions"]["script"], "create",
                        "--cwd", cwd,
                        "--source", cfg["claude_sessions"]["source"]],
                       capture_output=True, text=True, timeout=90,
                       env=_session_env())
    try:
        return _json.loads(r.stdout)["id"]
    except (ValueError, KeyError):
        raise RuntimeError(f"session create failed: "
                           f"{(r.stderr or r.stdout or '').strip()[-300:]}")


def _find_peer(sid: str) -> str | None:
    """Exact recipient id of session-<sid> once its comms receiver is online.
    `comms who` may print the alias bare or host-qualified (<host>:session-x)."""
    import json as _json
    who = subprocess.run(["comms", "who", "--compact"],
                         capture_output=True, text=True, timeout=15)
    if who.returncode:
        return None
    try:
        peers = _json.loads(who.stdout)
    except ValueError:
        return None
    peer = next((p for p in peers
                 if str(p.get("address", "")).rsplit(":", 1)[-1] == f"session-{sid}"
                 and p.get("online")), None)
    return peer["recipient"] if peer else None


def _post_task(cfg, recipient: str, sid: str, task: str) -> str:
    """Submit the task with an idempotent id; returns '' or the error."""
    posted = subprocess.run(["comms", "post", "--from", cfg["comms"]["alias"],
                             "--to", recipient, "--id", f"brain_spawn_{sid}",
                             "--stdin", "--compact"], input=task,
                            capture_output=True, text=True, timeout=15)
    if posted.returncode:
        return posted.stderr.strip() or "post failed"
    return ""


def kill_session(cfg, sid: str) -> str:
    r = subprocess.run([cfg["claude_sessions"]["script"], "kill", sid],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or r.stderr.strip() or f"killed {sid}"


def _after_final(cfg) -> str:
    """Worker instructions for after its [FINAL] report."""
    idle = cfg["claude_sessions"].get("idle_minutes", 0) or 0
    if idle <= 0:
        return ("After the final report is sent (and any handoffs "
                "acknowledged), close comms and end this session with /exit — "
                "finished workers are reaped.")
    return (f"After the final report, do NOT close comms or /exit: stay idle "
            f"and keep your context, since follow-ups may arrive over comms "
            f"for about {idle:g} minutes. Answer a follow-up like the original "
            f"task (start its last report with {_final_mark()} again). The "
            f"session is closed for you once it stays idle that long.")


def _final_mark() -> str:
    from .inbox import FINAL_MARK
    return FINAL_MARK


SPAWN_POLL_SECONDS = 3
SPAWN_POLL_ATTEMPTS = 20


def _spawn_job(origin: dict, task_text: str, cwd: str, cfg, db):
    """Background half of claude_spawn. Success: the worker is tasked and its
    later messages route to the origin thread (brain/inbox.py). Failure: a
    system note lands in the origin thread so the requester is told."""
    from . import inbox
    brain = inbox.current()
    sid = None

    def fail(reason):
        db.step(origin["turn_id"], "spawn_failed", channel=origin["channel"],
                session=sid, reason=reason)
        if sid:
            brain.store.set_worker(sid, state="failed")
        brain.submit({"channel": origin["channel"], "thread": origin["thread"],
                      "sender": "system (claude_spawn)", "tier": AGENT,
                      "text": _spawn_failed(reason),
                      "provider_id": f"spawn_failed:{origin['turn_id']}:{sid}"},
                     route="deliver", meta={"deliver_to": origin["channel"]},
                     kind="system")

    try:
        sid = _create_session(cfg, cwd)
    except Exception as e:
        return fail(str(e) or type(e).__name__)
    brain.store.add_worker(sid, origin["channel"], origin["thread"],
                           origin["sender"], task_text)
    task = (f"{cfg['assistant_name']} ({cfg['owner_name']}'s assistant) spawned "
            f"you for a task that came from channel {origin['channel']}. Report "
            f"progress sparingly via comms to {cfg['comms']['alias']}; start your "
            f"final report with {inbox.FINAL_MARK}. Each report is relayed to "
            f"that channel. {_after_final(cfg)} Task: {task_text}")
    # Wait for the exact new local agent's harness-owned receiver, then
    # submit to its immutable id with an idempotent task id.
    for attempt in range(SPAWN_POLL_ATTEMPTS):
        time.sleep(SPAWN_POLL_SECONDS)
        dead = _pane_problem(sid, settled=attempt >= 2)
        if dead:
            return fail(f"session {sid}: {dead}")
        recipient = _find_peer(sid)
        if not recipient:
            continue
        brain.store.set_worker(sid, recipient=recipient, state="running")
        err = _post_task(cfg, recipient, sid, task)
        if err:
            return fail(f"session {sid} joined, but task submission failed or "
                        f"is uncertain ({err}); check message brain_spawn_{sid}")
        db.step(origin["turn_id"], "session_spawned", channel=origin["channel"],
                session=sid, cwd=cwd, task=task_text)
        return None
    return fail(f"session {sid} created but its comms receiver never came "
                f"online (tmux attach -t {sid})")


def t_claude_spawn(env, args, cfg, db):
    """Managed Claude Code session (scripts/claude-sessions.sh): tmux + remote
    control + comms membership. Asynchronous: returns at once; the worker's
    reports arrive later as messages in this conversation."""
    from . import inbox
    if inbox.current() is None:
        return _spawn_failed("the inbox dispatcher is not running")
    origin = {"channel": env.get("origin") or env["channel"],
              "thread": env.get("thread") or "", "sender": env["sender"],
              "turn_id": env.get("turn_id", "")}
    cwd = args.get("cwd") or os.path.expanduser("~")
    threading.Thread(target=_spawn_job, args=(origin, args["task"], cwd, cfg, db),
                     daemon=True, name="claude-spawn").start()
    return ("Worker starting in the background. Delegation is asynchronous: "
            "the worker's result arrives later as a new message in this "
            "conversation, in a new turn. Tell the requester the work has "
            "started and end this turn; there is nothing to wait for or poll.")


def _comms_json(result, limit=6000) -> str:
    import json as _json
    if not result["ok"]:
        return result["error"]
    out = _json.dumps(result["result"], ensure_ascii=False)
    return out if len(out) <= limit else out[:limit] + " ... (truncated)"


def t_comms_who(env, args, cfg, db):
    from . import comms_v1
    r = comms_v1.who()
    if r["ok"]:
        # node rows: {machine_id, agent_id, peer_alias, alias, persistent, online}
        r = {"ok": True, "result": [
            {"address": (f"{p['peer_alias']}:{p['alias']}" if p.get("peer_alias")
                         else p.get("alias")),
             "recipient": f"{p.get('machine_id')}:{p.get('agent_id')}",
             "persistent": bool(p.get("persistent")),
             "online": bool(p.get("online"))}
            for p in r["result"] or []]}
    return _comms_json(r)


def t_comms_status(env, args, cfg, db):
    from . import comms_v1
    return _comms_json(comms_v1.message_status(str(args.get("message_id", "")).strip()))


def _compact_history(r):
    """Node history rows -> the CLI's compact shape (exact from/to ids)."""
    if not r["ok"]:
        return r
    out = {"messages": [
        {"id": m.get("id"),
         "from": f"{m.get('sender_machine_id')}:{m.get('sender_agent_id')}",
         "to": f"{m.get('recipient_machine_id')}:{m.get('recipient_agent_id')}",
         "state": m.get("state"), "created_at": m.get("created_at"),
         "body": m.get("body")}
        for m in r["result"].get("messages", [])]}
    if r["result"].get("next_cursor"):
        out["next_cursor"] = r["result"]["next_cursor"]
    return {"ok": True, "result": out}


def t_comms_log(env, args, cfg, db):
    from . import comms_v1
    return _comms_json(_compact_history(comms_v1.history(
        peer=args.get("peer") or None, limit=args.get("limit") or 20,
        cursor=args.get("cursor") or None)))


def t_comms_inbox(env, args, cfg, db):
    from . import comms_v1
    return _comms_json(_compact_history(comms_v1.history(
        limit=args.get("limit") or 20, pending=True)))


def t_claude_list(env, args, cfg, db):
    r = subprocess.run([cfg["claude_sessions"]["script"], "list", "--json"],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or "[]"


def _norm_sid(sid):
    """The registry keys sessions as '<source>-<hex>' (e.g. brain-8dcb3f5a),
    but workers self-identify on comms as 'session-<id>' / '<host>:session-<id>'
    — accept any of those spellings."""
    sid = sid.strip()
    if sid.startswith("comms:"):
        sid = sid[len("comms:"):]
    if ":session-" in sid:
        sid = sid.split(":", 1)[1]
    if sid.startswith("session-"):
        sid = sid[len("session-"):]
    return sid


def t_claude_kill(env, args, cfg, db):
    from . import inbox
    sid = _norm_sid(args["session_id"])
    brain = inbox.current()
    if brain and brain.store.worker(sid):
        brain.store.set_worker(sid, state="reaped", idle_until=None)
    return kill_session(cfg, sid)


def t_tool_log(env, args, cfg, db):
    import json as _json
    tid = str(args.get("turn_id", "")).strip()
    if not tid:
        return "tool_log needs a turn_id from the tool-call record"
    rows = db.tool_steps(tid)
    if not rows:
        return f"no tool calls recorded for turn {tid}"
    out = "\n".join(_json.dumps(r, ensure_ascii=False)[:3000] for r in rows)
    return out[:9000]


def t_read_thread(env, args, cfg, db):
    msgs = db.window(args["channel"], int(args.get("n", 10)))
    if not msgs:
        return "(empty thread)"
    return "\n".join(f"[{m['sender']}] {m['text']}" for m in msgs)


def t_schedule(env, args, cfg, db):
    fire = time.time() + float(args["minutes"]) * 60
    db.add_timer(fire, env["channel"], args["message"],
                 tier=env["tier"], sender=env["sender"])
    return f"scheduled for {args['minutes']} min from now"


def timer_envelope(t: dict, language: str) -> dict:
    """A due timer fires at the tier of whoever scheduled it, never higher.
    Rows from before tiers were recorded fire as the most restrictive tier."""
    tier = t.get("tier") if t.get("tier") in RING and t.get("tier") != AGENT \
        else "unknown"
    who = t.get("sender") or "unknown sender"
    return dict(channel=t["channel"], sender=f"timer set by {who}", tier=tier,
                provider_id=f"timer:{t['id']}",
                text=f"[reminder due] Deliver this reminder now, in {language}, "
                     f"on this channel: {t['message']}")


# -- calendar (external script, see README "Calendar") ------------------------
# Reads all of the owner's calendars merged; writes ONLY to the primary calendar.
# Writes are two-phase: calendar_write STAGES the operation and returns a
# pending id + human summary; calendar_confirm EXECUTES it after the person
# confirmed. The staged payload is what runs — it cannot drift between the
# question and the confirmation. Actions are a whitelist and every argument
# travels as argv (never through a shell).

CAL_PENDING_TTL = 600  # s

_cal_pending = {}      # pending_id -> dict(argv, summary, channel, ts)
_cal_guard = threading.Lock()


def _gcal(cfg, argv, timeout=60):
    try:
        r = subprocess.run([cfg["calendar"]["script"]] + argv, capture_output=True, text=True,
                           timeout=timeout)
    except Exception as e:
        return False, f'{{"error":"calendar script unavailable: {e}"}}'
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return False, err or out or f'{{"error":"calendar rc={r.returncode}"}}'
    return True, out or "{}"


def _cal_build_times(start: str, end: str | None, tz: str) -> dict:
    """Calendar event time fields from ISO strings. Timed: end defaults to +1h.
    All-day (date only): Google's end date is exclusive, so +1 day."""
    if "T" in start:
        s = datetime.datetime.fromisoformat(start)
        e = (datetime.datetime.fromisoformat(end) if end
             else s + datetime.timedelta(hours=1))
        if e <= s:
            raise ValueError("end must be after start")
        return {"start": {"dateTime": s.isoformat(), "timeZone": tz},
                "end": {"dateTime": e.isoformat(), "timeZone": tz}}
    s = datetime.date.fromisoformat(start)
    e = (datetime.date.fromisoformat(end) if end and "T" not in end
         else s) + datetime.timedelta(days=1)
    if e <= s:
        raise ValueError("end must be after start")
    return {"start": {"date": s.isoformat()}, "end": {"date": e.isoformat()}}


def t_calendar_read(env, args, cfg, db):
    action = args.get("action", "")
    days = max(1, min(int(args.get("days") or 0) or 7, 60))
    if action in ("today", "week"):
        argv = [action]
    elif action == "upcoming":
        argv = ["upcoming", str(days)]
    elif action == "search":
        q = str(args.get("query", "")).strip()
        if not q:
            return "search needs a query"
        argv = ["search", q, str(max(1, min(int(args.get("days") or 0) or 30, 90)))]
    else:
        return "unknown action (today|week|upcoming|search)"
    ok, out = _gcal(cfg, argv)
    if len(out) > 6000:
        out = out[:6000] + "\n... (truncated — refine with search)"
    return out


def calendar_pending(channel: str):
    """Latest staged-but-unconfirmed change for this channel, for the system
    prompt: the model must SEE that a confirmation is in flight (tool results
    don't survive into the next turn, and perception can't be a tool call)."""
    with _cal_guard:
        now = time.time()
        for k in [k for k, v in _cal_pending.items()
                  if now - v["ts"] > CAL_PENDING_TTL]:
            del _cal_pending[k]
        mine = [v for v in _cal_pending.values() if v["channel"] == channel]
    return max(mine, key=lambda v: v["ts"])["summary"] if mine else None


def _cal_stage(env, db, argv, summary):
    pid = f"cal_{uuid.uuid4().hex[:8]}"
    with _cal_guard:
        now = time.time()
        for k in [k for k, v in _cal_pending.items()
                  if now - v["ts"] > CAL_PENDING_TTL]:
            del _cal_pending[k]
        for v in _cal_pending.values():
            # re-staging the same op means the model lost track of the flow
            # (e.g. the person already said yes) — don't loop, point at confirm
            if v["channel"] == env["channel"] and v["argv"] == argv:
                v["ts"] = now
                return (f"already staged, awaiting confirmation: {v['summary']}\n"
                        f"If the person's latest message IS the confirmation, "
                        f"call calendar_confirm NOW instead of asking again.")
        _cal_pending[pid] = dict(argv=argv, summary=summary,
                                 channel=env["channel"], ts=now)
    db.step(env["turn_id"], "calendar_staged", channel=env["channel"],
            pending_id=pid, argv=argv, sender=env["sender"])
    return (f"STAGED (nothing executed yet): {summary}\n"
            f"Show this to the person and ask for confirmation. Only after "
            f"an explicit yes in their NEXT message, call calendar_confirm "
            f"(no arguments needed — it picks up this staged change). If "
            f"they decline, call calendar_confirm with cancel=true.")


def t_calendar_write(env, args, cfg, db):
    import json as _json
    action = args.get("action", "")
    if action == "create":
        title = str(args.get("summary", "")).strip()
        if not title or not args.get("start"):
            return "create needs summary and start"
        try:
            body = _cal_build_times(str(args["start"]),
                                    args.get("end") or None, cfg["timezone"])
        except ValueError as e:
            return f"bad start/end: {e}"
        body["summary"] = title
        for f in ("location", "description"):
            if args.get(f):
                body[f] = str(args[f])
        when = args["start"] + (f" – {args['end']}" if args.get("end") else "")
        return _cal_stage(env, db, ["create", _json.dumps(body)],
                          f"CREATE '{title}' at {when}")
    if action in ("update", "delete"):
        eid = str(args.get("event_id", "")).strip()
        if not eid:
            return f"{action} needs event_id (find it with calendar_read search)"
        # writes are pinned to the primary calendar: shared/busy calendars
        # are untouchable by construction
        ok, cur = _gcal(cfg, ["get", "primary", eid])
        if not ok:
            return (f"event {eid} not found on the primary calendar: {cur} "
                    f"— only the owner's own (primary) events can be changed")
        if action == "delete":
            return _cal_stage(env, db, ["delete", "primary", eid],
                              f"DELETE the event: {cur[:300]}")
        patch = {}
        if args.get("summary"):
            patch["summary"] = str(args["summary"])
        if args.get("start"):
            try:
                patch.update(_cal_build_times(str(args["start"]),
                                              args.get("end") or None,
                                              cfg["timezone"]))
            except ValueError as e:
                return f"bad start/end: {e}"
        for f in ("location", "description"):
            if args.get(f):
                patch[f] = str(args[f])
        if not patch:
            return "update needs at least one of summary/start/end/location/description"
        return _cal_stage(env, db, ["update", "primary", eid, _json.dumps(patch)],
                          f"CHANGE to {_json.dumps(patch, ensure_ascii=False)[:200]} "
                          f"the event: {cur[:300]}")
    return "unknown action (create|update|delete)"


def t_calendar_confirm(env, args, cfg, db):
    pid = str(args.get("pending_id") or "").strip()
    with _cal_guard:
        now = time.time()
        for k in [k for k, v in _cal_pending.items()
                  if now - v["ts"] > CAL_PENDING_TTL]:
            del _cal_pending[k]
        if not pid:
            # tool results don't survive into the next turn's window, so the
            # model usually can't repeat the id — default to the latest
            # operation staged from THIS conversation (the channel binding
            # is the actual security key)
            mine = [(v["ts"], k) for k, v in _cal_pending.items()
                    if v["channel"] == env["channel"]]
            if not mine:
                return "nothing pending for this conversation (expired or already executed) — stage it again"
            pid = max(mine)[1]
        p = _cal_pending.get(pid)
        if not p:
            return "no such pending operation (expired, executed, or wrong id) — stage it again"
        if p["channel"] != env["channel"]:
            return "denied: a staged change can only be confirmed from the conversation that asked for it"
        del _cal_pending[pid]
    if args.get("cancel"):
        db.step(env["turn_id"], "calendar_cancelled", channel=env["channel"],
                pending_id=pid)
        return f"cancelled, nothing executed: {p['summary']}"
    ok, out = _gcal(cfg, p["argv"])
    db.step(env["turn_id"], "calendar_executed", channel=env["channel"],
            pending_id=pid, argv=p["argv"], ok=ok, sender=env["sender"])
    return out if ok else f"execution failed: {out}"


# -- WhatsApp re-pairing --------------------------------------------------------
# The sidecar serves its pairing state on localhost (GET /status, GET /qr,
# POST /repair). whatsapp_pair restarts pairing and relays each new QR to the
# channel that asked — never over WhatsApp, which is the thing being repaired.
WA_PAIR_TIMEOUT = 180  # s; QR codes rotate about every 20 s
WA_PAIR_POLL = 3       # s


def _wa_http(cfg, method: str, path: str, timeout: float = 10):
    """(status, json body) from the sidecar; (0, {"error": ...}) when unreachable."""
    import json as _json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg['whatsapp']['port']}{path}", method=method,
        data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


# injectable for tests: HTTP to the sidecar, sleeping, and the clock
_wa = {"http": _wa_http, "sleep": time.sleep, "clock": time.monotonic}


def t_whatsapp_pair(env, args, cfg, db):
    channel = env["channel"]
    if channel.startswith("wpp:"):
        return ("refused: pairing QR codes are never sent over WhatsApp. Ask for "
                "pairing from the CLI or a comms session.")
    http, sleep, clock = _wa["http"], _wa["sleep"], _wa["clock"]
    status, body = http(cfg, "POST", "/repair")
    if status != 200:
        return (f"whatsapp pairing FAILED: sidecar /repair returned {status} "
                f"{body.get('error', '')}. Is the brain-wa service running?").strip()
    db.step(env["turn_id"], "whatsapp_repair", channel=channel,
            sender=env["sender"], moved_to=body.get("moved_to"))
    deadline = clock() + WA_PAIR_TIMEOUT
    last_qr, sent = None, 0
    while clock() < deadline:
        status, st = http(cfg, "GET", "/status")
        if status == 200 and st.get("connected"):
            db.step(env["turn_id"], "whatsapp_paired", channel=channel,
                    jid=st.get("jid"), qr_sent=sent)
            return f"connected as {st.get('jid')}"
        status, q = http(cfg, "GET", "/qr")
        if status == 200 and q.get("qr") and q["qr"] != last_qr:
            last_qr = q["qr"]
            sent += 1
            out = deliver(channel,
                          f"WhatsApp QR #{sent}: on the phone open WhatsApp > "
                          f"Linked devices > Link a device and scan it (it "
                          f"rotates in ~20 s; a new one follows):\n"
                          f"{q.get('text') or q['qr']}",
                          "whatsapp_pair", cfg, db)
            if out.startswith("unknown or disabled channel"):
                return (f"whatsapp pairing FAILED: can't deliver QR codes to "
                        f"{channel}. Ask from the CLI or a comms session.")
            if channel == "cli":
                # cli deliveries are only recorded; show it on the terminal
                # (python -m brain.cli) or in data/brain.log (service)
                print(f"[whatsapp_pair] QR #{sent}:\n{q.get('text') or q['qr']}",
                      flush=True)
        sleep(WA_PAIR_POLL)
    db.step(env["turn_id"], "whatsapp_pair_timeout", channel=channel, qr_sent=sent)
    return (f"timed out after {WA_PAIR_TIMEOUT} s without a scan ({sent} QR "
            f"codes sent). Ask again to retry.")


TOOLS = [
    dict(name="remember", ring=2, fn=t_remember,
         description="Saves a lasting fact to durable memory, attributed to "
                     "the speaker.",
         parameters={"type": "object", "properties": {
             "fact": {"type": "string"}}, "required": ["fact"]}),
    dict(name="send_to", ring=0, fn=t_send_to, agent=True,
         description="Sends a message to a channel: a person (wpp:<alias>), "
                      "an agent (comms-v1:<recipient> or comms-v1:<address>), "
                      "or cli. The reply to the current speaker is delivered "
                      "automatically, so send_to is for other channels. For "
                      "comms it returns the message id and delivery state.",
         parameters={"type": "object", "properties": {
             "channel": {"type": "string",
                         "description": "a channel from the Contacts section "
                                        "of your context (wpp:<alias> | "
                                        "comms-v1:<machine>:<agent> | cli)"},
             "message": {"type": "string"}}, "required": ["channel", "message"]}),
    dict(name="tool_log", ring=2, fn=t_tool_log,
         description="Full input and output of a past turn's tool calls. The "
                      "turn_id is at the end of each line of the tool-call "
                      "record.",
         parameters={"type": "object", "properties": {
             "turn_id": {"type": "string"}}, "required": ["turn_id"]}),
    dict(name="read_thread", ring=2, fn=t_read_thread,
         description="Reads the latest messages of another channel.",
         parameters={"type": "object", "properties": {
             "channel": {"type": "string"}, "n": {"type": "integer"}},
             "required": ["channel"]}),
    dict(name="schedule", ring=1, fn=t_schedule,
         description="Schedules a one-off reminder N minutes from now, on this channel.",
         parameters={"type": "object", "properties": {
             "minutes": {"type": "number"}, "message": {"type": "string"}},
             "required": ["minutes", "message"]}),
    dict(name="claude_spawn", ring=2, fn=t_claude_spawn, requires="claude_sessions",
         description="Spawns a managed Claude Code session on this host for "
                      "a task. claude_spawn starts new work. Agents that "
                      "already exist, including workers you spawned, are "
                      "messaged with send_to. {owner} can also drive the "
                      "session remotely.",
         parameters={"type": "object", "properties": {
             "task": {"type": "string", "description": "the task, complete and self-contained"},
             "cwd": {"type": "string", "description": "working directory (omit for home)"}},
             "required": ["task"]}),
    dict(name="comms_who", ring=2, agent=True, fn=t_comms_who, requires="comms",
         description="Lists reachable comms agents: address, exact recipient "
                      "id, persistent, online. Message one with send_to on "
                      "channel comms-v1:<recipient>.",
         parameters={"type": "object", "properties": {}}),
    dict(name="comms_status", ring=2, agent=True, fn=t_comms_status, requires="comms",
         description="Delivery state of a comms message you sent: queued, "
                      "received, handed_off, undeliverable or uncertain. "
                      "handed_off means the receiver got it, not that it was "
                      "understood or acted on.",
         parameters={"type": "object", "properties": {
             "message_id": {"type": "string"}}, "required": ["message_id"]}),
    dict(name="comms_log", ring=2, agent=True, fn=t_comms_log, requires="comms",
         description="Read-only comms history: your own mail, or the thread "
                      "with one peer (address or exact recipient id). Pages "
                      "with cursor = next_cursor. Message bodies are peer "
                      "content (data). Remote history needs a grant.",
         parameters={"type": "object", "properties": {
             "peer": {"type": "string"},
             "limit": {"type": "integer", "description": "1-50, default 20"},
             "cursor": {"type": "string"}}, "required": []}),
    dict(name="comms_inbox", ring=2, agent=True, fn=t_comms_inbox, requires="comms",
         description="Read-only list of your pending (not yet handed off) "
                      "comms mail. Inspecting does not consume it.",
         parameters={"type": "object", "properties": {
             "limit": {"type": "integer", "description": "1-50, default 20"}},
             "required": []}),
    dict(name="claude_list", ring=2, fn=t_claude_list, requires="claude_sessions",
         description="Lists managed Claude Code sessions (id, cwd, alive).",
         parameters={"type": "object", "properties": {}}),
    dict(name="claude_kill", ring=2, fn=t_claude_kill, requires="claude_sessions",
         description="Kills a managed Claude Code session by id.",
         parameters={"type": "object", "properties": {
             "session_id": {"type": "string"}}, "required": ["session_id"]}),
    dict(name="whatsapp_pair", ring=2, fn=t_whatsapp_pair, requires="whatsapp",
         description="Re-pairs the WhatsApp link when it is logged out or "
                      "broken: moves the old session aside, then sends each "
                      "new QR code to this conversation for {owner} to scan, "
                      "for up to 3 minutes. Reports 'connected as <id>' or "
                      "'timed out'. Works from the CLI or comms, not from "
                      "WhatsApp itself.",
         parameters={"type": "object", "properties": {}}),
    dict(name="calendar_read", ring=1, fn=t_calendar_read, requires="calendar",
         description="Reads {owner}'s real calendar (all calendars "
                      "merged, timezone {tz}). Returns JSON "
                      "events {{calendar, summary, location, start, end, id, "
                      "link}}. Write dates the way people do in the reply "
                      "language and show times only for timed events. An "
                      "event whose summary is null is a busy block from a "
                      "shared calendar: name the calendar and say it is busy.",
         parameters={"type": "object", "properties": {
             "action": {"type": "string",
                        "enum": ["today", "week", "upcoming", "search"]},
             "days": {"type": "integer",
                      "description": "window for upcoming/search"},
             "query": {"type": "string", "description": "search text"}},
             "required": ["action"]}),
    dict(name="calendar_write", ring=1, fn=t_calendar_write, requires="calendar",
         description="Stages a change to {owner}'s primary calendar. Nothing "
                      "runs until calendar_confirm, and calendar_confirm needs "
                      "the person's explicit yes in a later message: show the "
                      "returned summary and ask. For update/delete, find the "
                      "event with calendar_read search and pass its id; only "
                      "events on the primary calendar can change (shared and "
                      "busy ones are read-only). A timed event without an end "
                      "lasts 1h; a date without a time is an all-day event. "
                      "start/end are ISO: '2026-06-12T15:00:00' or '2026-06-20'.",
         parameters={"type": "object", "properties": {
             "action": {"type": "string",
                        "enum": ["create", "update", "delete"]},
             "summary": {"type": "string", "description": "event title"},
             "start": {"type": "string"}, "end": {"type": "string"},
             "location": {"type": "string"},
             "description": {"type": "string"},
             "event_id": {"type": "string",
                          "description": "for update/delete, from calendar_read"}},
             "required": ["action"]}),
    dict(name="calendar_confirm", ring=1, fn=t_calendar_confirm, requires="calendar",
         description="Runs the calendar change staged by calendar_write in "
                      "this conversation, or cancels it with cancel=true. It "
                      "requires the person's explicit yes in a message after "
                      "the staging turn.",
         parameters={"type": "object", "properties": {
             "cancel": {"type": "boolean"}},
             "required": []}),
]


def available(cfg) -> list[dict]:
    """Tools whose integration is enabled; disabled integrations expose nothing."""
    return [t for t in TOOLS
            if not t.get("requires") or enabled(cfg, t["requires"])]


def allowed(tool: dict, tier: str) -> bool:
    """Tier policy. The agent tier (worker / peer agent turns, never owner)
    gets only tools flagged agent=True; every other tier goes by ring."""
    if tier == AGENT:
        return bool(tool.get("agent"))
    return RING.get(tier, 0) >= tool["ring"]


def schema_for(tier: str, cfg) -> list[dict]:
    """Envelope-scoped tool exposure: tools the tier can't use aren't in the
    schema at all."""
    return [{"type": "function", "name": t["name"],
             "description": t["description"].format(owner=cfg["owner_name"],
                                                    tz=cfg["timezone"]),
             "parameters": t["parameters"]}
            for t in available(cfg) if allowed(t, tier)]


def execute(name: str, env: dict, args: dict, cfg, db) -> str:
    tool = next((t for t in available(cfg) if t["name"] == name), None)
    if tool is None:
        return f"unknown tool: {name}"
    if not allowed(tool, env["tier"]):  # defense in depth
        db.step(env["turn_id"], "policy_denial", channel=env["channel"],
                tool=name, sender=env["sender"])
        return "denied by policy"
    return tool["fn"](env, args, cfg, db)
