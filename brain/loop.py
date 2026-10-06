"""The agent loop: envelope in → context → model+tools → reply out.

Globally serialized (one turn at a time) by design — race-free memory,
coherent blackboard. Every step traced to SQLite. Turns are started by the
inbox dispatcher (brain/inbox.py); before each model step the running turn
merges new messages from the same sender and thread."""

import json
import os
import threading
import time
import uuid

from openai import OpenAI

from . import prompts, tools
from .config import api_key, enabled, message
from .db import DB
from .inbox import MAX_DRAFT_DISCARDS

def reasoning_tokens(usage) -> int:
    """Reasoning tokens are a subset of output_tokens (already in _cost)."""
    return getattr(getattr(usage, "output_tokens_details", None),
                   "reasoning_tokens", 0) or 0


def _cost(cfg, usage) -> float:
    return (usage.input_tokens * cfg["price_in_per_mtok"]
            + usage.output_tokens * cfg["price_out_per_mtok"]) / 1e6


def run_turn(envelope: dict, cfg: dict, db: DB, client: OpenAI | None = None,
             on_start=None) -> str:
    """envelope: {channel, sender, tier, text[, thread, provider_id]}

    Writes the envelope to the inbox and blocks until a turn has answered it
    (one turn at a time, globally). For callers that block anyway: the CLI,
    `"wait": true` HTTP requests. on_start is accepted for compatibility;
    pollers see the row's state instead."""
    from . import inbox
    brain = inbox.get(cfg, db,
                      client_factory=(lambda: client) if client else None)
    return brain.run_sync(envelope)


def _merge(rows, env, db, turns_input):
    """Append merged inbox rows as user messages, each with its own stamp."""
    from .inbox import stamp
    for r in rows:
        text = stamp(r)
        turns_input.append({"role": "user", "content": text})
        db.add_message(env["channel"], "user", r["sender"], r["text"], r["tier"])
        db.step(env["turn_id"], "merged", channel=env["channel"],
                inbox_id=r["id"], sender=r["sender"], tier=r["tier"],
                kind=r["kind"])


def _run(env, cfg, db, client, pull=None, renew=None, turn_id=None):
    turn_id = turn_id or f"t_{uuid.uuid4().hex[:10]}"
    env = dict(env, turn_id=turn_id)
    pull = pull or (lambda: [])
    renew = renew or (lambda: None)
    t0 = time.time()
    db.step(turn_id, "envelope", channel=env["channel"],
            sender=env["sender"], tier=env["tier"], text=env["text"])

    # spending caps are enforced by the dispatcher before a turn starts
    # (inbox.Brain._hold): a message is never consumed by a refusal here
    db.add_message(env["channel"], "user", env["sender"], env["text"],
                   env["tier"])

    board = db.blackboard(env["channel"], cfg["blackboard_hours"],
                          cfg["blackboard_max_lines"])
    window = db.window(env["channel"], cfg["window_turns"])
    ident = None
    if enabled(cfg, "comms"):
        from . import comms_v1
        r = comms_v1.identity()
        ident = r["result"] if r["ok"] else None
    meta = env.get("meta") or {}
    worker = None
    if env.get("kind") in ("worker_result", "system"):
        worker = {"kind": ("final report" if meta.get("final") else "progress report")
                  if env["kind"] == "worker_result" else "runtime notice",
                  "sid": meta.get("worker_sid") or "?",
                  "origin": env.get("origin") or env["channel"],
                  "request": (meta.get("request") or "")[:200]}
    # Past tool calls belong in the system record, never as assistant turns:
    # shown as assistant content, the model imitates the trace format and
    # narrates fake tool calls instead of emitting real ones.
    traces = [m["text"] for m in window[:-1] if m["role"] == "tool"][-8:]
    system, stats = prompts.compose(
        env, cfg, db, board=board, book=tools.contacts(cfg), traces=traces,
        pending_cal=tools.calendar_pending(env["channel"]),
        comms_identity=ident, worker=worker)

    turns_input = []
    for m in window[:-1]:
        if m["role"] == "tool":
            continue  # rendered in the system record above, not as a turn
        turns_input.append({"role": "user" if m["role"] == "user" else "assistant",
                            "content": (f"[{m['sender']}] " if m["role"] == "user" else "")
                            + m["text"]})
    turns_input.append({"role": "user",
                        "content": f"[{env['sender']}] {env['text']}"})

    db.step(turn_id, "context", channel=env["channel"],
            window=len(window), blackboard=len(board),
            memory_chars=stats["memory_chars"], system_chars=len(system))

    schema = tools.schema_for(env["tier"], cfg)
    final = ""
    steps, budget, discards = 0, cfg["max_tool_steps"], 0
    while steps < budget:
        renew()
        t_model = time.time()
        resp = client.responses.create(
            model=cfg["model"], instructions=system, input=turns_input,
            tools=schema or None,
            reasoning={"effort": cfg["reasoning_effort"]})
        u = resp.usage
        db.step(turn_id, "model", channel=env["channel"], model=cfg["model"],
                ms=int((time.time() - t_model) * 1000),
                tokens_in=u.input_tokens, tokens_out=u.output_tokens,
                cost_usd=_cost(cfg, u), status=resp.status,
                reasoning_effort=cfg["reasoning_effort"],
                reasoning_tokens=reasoning_tokens(u))
        steps += 1
        calls = [it for it in resp.output if it.type == "function_call"]
        if not calls:
            draft = resp.output_text or ""
            late = pull()
            if late and discards < MAX_DRAFT_DISCARDS:
                # the speaker added something while we were writing: drop the
                # draft and take one more step with the new message in view
                discards += 1
                db.step(turn_id, "draft_discarded", channel=env["channel"],
                        merged=len(late), draft=draft[:500])
                _merge(late, env, db, turns_input)
                budget = max(budget, steps + 1)
                continue
            if late:
                _merge(late, env, db, turns_input)  # answered by this reply
            final = draft
            break
        turns_input += [it for it in resp.output]
        for fc in calls:
            args = json.loads(fc.arguments or "{}")
            t_tool = time.time()
            result = tools.execute(fc.name, env, args, cfg, db)
            db.step(turn_id, "tool", channel=env["channel"],
                    ms=int((time.time() - t_tool) * 1000),
                    name=fc.name, input=args, output=result)
            turns_input.append({"type": "function_call_output",
                                "call_id": fc.call_id, "output": result})
            # persist a cropped trace in the thread so future turns SEE that
            # (and which) tools ran; tool_log(turn_id) fetches the full record
            in_crop = json.dumps(args, ensure_ascii=False)[:160]
            db.add_message(env["channel"], "tool", f"tool:{fc.name}",
                           f"{fc.name}({in_crop}) -> "
                           f"{result[:240]} [turn {turn_id}]")
        _merge(pull(), env, db, turns_input)   # step boundary

    if not final.strip():
        request = " ".join(env["text"].split())[:160]
        final = message(cfg, "out_of_steps" if steps >= budget else "failure",
                        request=request)
    db.add_message(env["channel"], "assistant", cfg["assistant_name"], final)
    db.step(turn_id, "reply", channel=env["channel"],
            ms=int((time.time() - t0) * 1000), text=final)
    _maybe_autocompact(env["channel"], cfg, db)
    return final


_compacting: set[str] = set()     # channels with a compaction in flight
_compacting_guard = threading.Lock()


def _maybe_autocompact(channel: str, cfg, db):
    """If a thread outgrows the limit, compact its older half in the background
    (same compact_channel the nightly cron uses — one mechanism, two triggers)."""
    limit = cfg.get("auto_compact_turns", 40)
    n = db.conn.execute("SELECT COUNT(*) FROM messages WHERE channel=?",
                        (channel,)).fetchone()[0]
    if n <= limit:
        return
    # compact down to the newest window_turns rows, so the next compaction
    # comes only after the thread grows past auto_compact_turns again
    keep = cfg["window_turns"]
    row = db.conn.execute(
        "SELECT ts FROM messages WHERE channel=? ORDER BY ts DESC "
        "LIMIT 1 OFFSET ?", (channel, keep - 1)).fetchone()
    if not row:
        return
    cutoff = row[0]

    with _compacting_guard:
        if channel in _compacting:
            return
        _compacting.add(channel)

    def job():
        from .compact import compact_channel
        from .config import api_key
        from .db import DB as _DB
        try:
            compact_channel(channel, cutoff, cfg, _DB(cfg["db_path"]),
                            OpenAI(api_key=api_key()))
        except Exception as e:
            print(f"[autocompact] {channel}: {e}")
        finally:
            with _compacting_guard:
                _compacting.discard(channel)

    threading.Thread(target=job, daemon=True).start()
