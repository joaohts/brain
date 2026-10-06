"""Dev channel: talk to the brain from any terminal.

  python -m brain.cli "hey there"          (owner tier by default)
  python -m brain.cli --tier unknown "oi"  (simulate a stranger)
"""

import argparse

from . import config
from .db import DB
from .loop import run_turn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--tier", default="owner",
                    choices=["owner", "family", "unknown"])
    ap.add_argument("--sender", help="default: <owner_name> (cli)")
    ap.add_argument("--channel", default="cli")
    a = ap.parse_args()

    cfg = config.load()
    db = DB(cfg["db_path"])
    a.sender = a.sender or f"{cfg['owner_name']} (cli)"
    reply = run_turn(dict(channel=a.channel, sender=a.sender,
                          tier=a.tier, text=a.text), cfg, db)
    print(reply)

    # one-shot process: wait for background jobs (spawn_claude etc.) so they
    # aren't killed at exit — the server doesn't need this, it never exits
    import threading
    for t in threading.enumerate():
        if t is not threading.main_thread() and t.is_alive():
            print(f"[cli] waiting for background job ({t.name}) ...")
            t.join()


if __name__ == "__main__":
    main()
