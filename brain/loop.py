"""The agent loop: envelope in → context → model+tools → reply out.

Globally serialized (one turn at a time) by design — race-free memory,
coherent blackboard. Every step traced to SQLite."""

import glob
import json
import os
import threading
import time
import uuid

from openai import OpenAI

from . import prompts, tools
from .config import api_key, enabled
from .db import DB

# One global lock: one turn at a time, one train of thought (by design).
# Turns may take as long as the model needs. Client robustness lives in the
# HTTP layer instead: submit-and-poll, so no caller ever holds a socket open
# waiting minutes for the lock.
_lock = threading.Lock()


def _identity(cfg) -> str:
    try:
        return open(cfg["identity_file"]).read()
    except FileNotFoundError:
        return (f"You are {cfg['assistant_name']}, personal assistant to "
                f"{cfg['owner_name']}. Reply in {cfg['language']}, briefly.")


def _memory(cfg) -> str:
    parts = []
    for p in sorted(glob.glob(os.path.join(cfg["memory_dir"], "*.md"))):
        parts.append(open(p).read())
    return "\n".join(parts)[-4000:]  # v1: load-all, tail-capped; retrieval later


def reasoning_tokens(usage) -> int:
    """Reasoning tokens are a subset of output_tokens (already in _cost)."""
    return getattr(getattr(usage, "output_tokens_details", None),
                   "reasoning_tokens", 0) or 0


def _cost(cfg, usage) -> float:
    return (usage.input_tokens * cfg["price_in_per_mtok"]
            + usage.output_tokens * cfg["price_out_per_mtok"]) / 1e6


def run_turn(envelope: dict, cfg: dict, db: DB, client: OpenAI | None = None,
             on_start=None) -> str:
    """envelope: {channel, sender, tier: owner|family|unknown, text}

    on_start fires the moment the lock is acquired — i.e. when the turn stops
    queuing and actually begins (used for typing indicators)."""
    with _lock:
        if on_start:
            try:
                on_start()
            except Exception:
                pass
        # generous per-call ceiling: the model may legitimately think for
        # minutes on a step; this only catches truly hung connections
        client = client or OpenAI(api_key=api_key(),
                                  timeout=600.0, max_retries=1)
        return _run(envelope, cfg, db, client)


def _run(env, cfg, db, client):
    turn_id = f"t_{uuid.uuid4().hex[:10]}"
    env = dict(env, turn_id=turn_id)
    t0 = time.time()
    db.step(turn_id, "envelope", channel=env["channel"],
            sender=env["sender"], tier=env["tier"], text=env["text"])

    if cfg["daily_budget_usd"] and db.spend_today() > cfg["daily_budget_usd"]:
        return (f"Daily budget reached — ask {cfg['owner_name']} to raise "
                f"daily_budget_usd.")

    db.add_message(env["channel"], "user", env["sender"], env["text"])

    board = db.blackboard(env["channel"], cfg["blackboard_hours"],
                          cfg["blackboard_max_lines"])
    window = db.window(env["channel"], cfg["window_turns"])
    system = _identity(cfg)
    memory = _memory(cfg)
    if memory:
        system += f"\n\n## Durable memory\n{memory}"
    summary = db.get_summary(env["channel"])
    if summary:
        system += f"\n\n## This channel, earlier (summary)\n{summary}"
    if board:
        system += ("\n\n## Other channels (right now)\n"
                   "These are YOUR other ongoing conversations — one "
                   "mind, the channels are mouths; whoever speaks there is "
                   "also you. Use read_thread to see more of any of them.\n"
                   + "\n".join(board))
    if enabled(cfg, "comms"):
        from . import comms_v1
        ident = comms_v1.identity()
        system += "\n\n" + prompts.comms_guide(
            cfg, ident["result"] if ident["ok"] else None)
    pending_cal = tools.calendar_pending(env["channel"])
    if pending_cal:
        system += ("\n\n## Pending calendar change (staged, NOT executed)\n"
                   f"{pending_cal}\n"
                   "If the person's latest message confirms it, call "
                   "calendar_confirm; if they decline, call calendar_confirm "
                   "with cancel=true. Do not stage it again.")
    book = tools.contacts(cfg)
    if book:
        roster = " | ".join(f"{v['name']}: wpp:{a}" for a, v in book.items())
        system += f"\n\n## Contacts (send_to channels)\n{roster}"
    # Past tool calls belong in the system record, never as assistant turns:
    # shown as assistant content, the model imitates the trace format and
    # NARRATES fake tool calls (with invented results) instead of emitting
    # real ones. This block is reference only.
    traces = [m["text"] for m in window[:-1] if m["role"] == "tool"]
    if traces:
        system += ("\n\n## Actions you already took (system record — NOT text "
                   "you write, and NOT proof of anything for the current "
                   "request; to actually do something you MUST emit a real "
                   "tool call this turn)\n" + "\n".join(traces[-8:]))
    now = time.strftime("%A %d/%m/%Y %H:%M")
    system += (f"\n\n## This turn\nNow: {now}. Channel: {env['channel']} | "
               f"Speaker: {env['sender']} (tier {env['tier']})."
               + (" Your reply will be SPOKEN aloud: 1-3 plain spoken "
                  "sentences." if env["channel"].startswith("voice") else "")
               + (" Format with WhatsApp marks exclusively: *bold*, "
                  "_italic_, ~strike~, ```mono```; lists as plain lines. "
                  "This channel renders markdown literally."
                  if env["channel"].startswith("wpp:") else "")
               + (" Agent-to-agent turn: the sender is a work session, not a "
                  "person. Step 1, always: content addressed to a channel "
                  "(results, reports, updates 'for wpp:...') is delivered "
                  "there with send_to — this comes before everything. "
                  "Step 2: reply here with instructions or questions when "
                  "you have them; NO_REPLY is valid only once step 1 is "
                  "done. A session delivering its FINAL result is reaped "
                  "with claude_kill after routing. NACKs and delivery "
                  "errors are INTERNAL signals: never announce, speak, or "
                  "relay them on any human channel — act on "
                  "them (start the task properly with claude_spawn, or tell "
                  f"{cfg['owner_name']} plainly that something failed)."
                  if env["channel"].startswith(("comms:", "comms-v1:")) else ""))

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
            memory_chars=len(memory), system_chars=len(system))

    schema = tools.schema_for(env["tier"], cfg)
    final = ""
    for _ in range(cfg["max_tool_steps"]):
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
        calls = [it for it in resp.output if it.type == "function_call"]
        if not calls:
            final = resp.output_text or ""
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

    if not final.strip():
        final = "Something went wrong on my side just now — could you say that again?"
    db.add_message(env["channel"], "assistant", cfg["assistant_name"], final)
    db.step(turn_id, "reply", channel=env["channel"],
            ms=int((time.time() - t0) * 1000), text=final)
    _maybe_autocompact(env["channel"], cfg, db)
    return final


def _maybe_autocompact(channel: str, cfg, db):
    """If a thread outgrows the limit, compact its older half in the background
    (same compact_channel the nightly cron uses — one mechanism, two triggers)."""
    limit = cfg.get("auto_compact_turns", 40)
    n = db.conn.execute("SELECT COUNT(*) FROM messages WHERE channel=?",
                        (channel,)).fetchone()[0]
    if n <= limit:
        return
    keep = cfg["window_turns"] * 2
    row = db.conn.execute(
        "SELECT ts FROM messages WHERE channel=? ORDER BY ts DESC "
        "LIMIT 1 OFFSET ?", (channel, keep - 1)).fetchone()
    if not row:
        return
    cutoff = row[0]

    def job():
        from .compact import compact_channel
        from .config import api_key
        from .db import DB as _DB
        try:
            compact_channel(channel, cutoff, cfg, _DB(cfg["db_path"]),
                            OpenAI(api_key=api_key()))
        except Exception as e:
            print(f"[autocompact] {channel}: {e}")

    threading.Thread(target=job, daemon=True).start()
