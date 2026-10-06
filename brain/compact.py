"""Nightly compaction: old thread turns → rolling channel summary + durable facts.

The "sleep" job. For each channel with messages older than the cutoff:
the model reads them (plus the existing summary), produces a merged summary and
any facts worth keeping forever; facts append to memory_dir/facts.md, the summary
replaces the channel's rolling summary, and the compacted rows are deleted
(full fidelity remains in the traces).

Run from cron:  15 4 * * *  cd /path/to/brain && .venv/bin/python -m brain.compact
Test:           python -m brain.compact --hours 0 --channel cli
"""

import argparse
import datetime
import json
import os
import time
import uuid

from openai import OpenAI

from . import config
from .db import DB
from .loop import _cost, reasoning_tokens

PROMPT = """You are the memory-consolidation process of the assistant {assistant}.
Input: the current summary of channel '{channel}' (may be empty) and old
messages about to be deleted. Output JSON with exactly these keys:
- "summary": updated running summary of the channel, merging the current
  summary with what the messages add. <=150 words. Facts and open items.
- "facts": list (may be empty) of DURABLE facts worth permanent memory
  (preferences, decisions, people, dates). Keep only what stays true.

## Current summary
{old_summary}

## Messages to compact
{messages}"""


def compact_channel(channel, cutoff, cfg, db, client):
    msgs = db.old_messages(channel, cutoff)
    if not msgs:
        return None
    rendered = "\n".join(
        f"[{datetime.datetime.fromtimestamp(m['ts']):%d/%m %H:%M} "
        f"{m['sender']}] {m['text']}" for m in msgs)[-12000:]
    turn_id = f"compact_{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    resp = client.responses.create(
        model=cfg["model"],
        input=PROMPT.format(assistant=cfg["assistant_name"], channel=channel,
                            old_summary=db.get_summary(channel) or "(empty)",
                            messages=rendered),
        reasoning={"effort": cfg["reasoning_effort"]},
        text={"format": {"type": "json_object"}})
    data = json.loads(resp.output_text)
    db.step(turn_id, "model", channel=channel, model=cfg["model"],
            ms=int((time.time() - t0) * 1000),
            tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
            cost_usd=_cost(cfg, resp.usage), job="compact",
            reasoning_effort=cfg["reasoning_effort"],
            reasoning_tokens=reasoning_tokens(resp.usage))

    db.set_summary(channel, data.get("summary", ""))
    facts = [f for f in data.get("facts", []) if f]
    if facts:
        os.makedirs(cfg["memory_dir"], exist_ok=True)
        with open(os.path.join(cfg["memory_dir"], "facts.md"), "a") as f:
            for fact in facts:
                f.write(f"- {datetime.date.today()} [{channel}]: {fact}\n")
    db.delete_old(channel, cutoff)
    db.step(turn_id, "compacted", channel=channel,
            messages=len(msgs), facts=len(facts))
    return dict(channel=channel, messages=len(msgs), facts=len(facts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0,
                    help="compact messages older than this")
    ap.add_argument("--channel", help="only this channel (testing)")
    a = ap.parse_args()

    cfg = config.load()
    db = DB(cfg["db_path"])
    client = OpenAI(api_key=config.api_key())
    cutoff = time.time() - a.hours * 3600

    channels = [a.channel] if a.channel else db.channels_with_old(cutoff)
    for ch in channels:
        r = compact_channel(ch, cutoff, cfg, db, client)
        if r:
            print(f"[compact] {r['channel']}: {r['messages']} msgs -> "
                  f"summary + {r['facts']} fact(s)")
    if not channels:
        print("[compact] nothing to do")


if __name__ == "__main__":
    main()
