"""HTTP door — submit-and-poll, so no client ever blocks on the brain's lock.

  POST /turn {channel, sender, tier, text[, thread, provider_id]}
                                                  -> 202 {"id": "m<n>", "duplicate": false}
  POST /turn {..., "wait": true}                  -> 200 {"reply": ...}  (blocking, for curl/tests)
  GET  /turn/<id>                                 -> {"status": "pending" | "running"}
                                                     {"status": "done", "reply": ...}
                                                     {"status": "error", "error": ...}

Every request is written to the inbox first (brain/inbox.py); turns answer
inbox rows one at a time. A message merged into a running turn of the same
sender finishes as "done" with an empty reply (the turn's own reply covers
it). provider_id is a dedup key: a repeat returns the original id with
"duplicate": true. Also runs the timer loop: due timers become inbox rows."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, inbox, tools
from .db import DB

cfg = config.load()
db = DB(cfg["db_path"])
brain = inbox.get(cfg, db)

_STATUS = {"unread": "pending", "read_by_turn": "running"}


def _view(row: dict | None) -> tuple[int, dict]:
    if row is None:
        return 404, {"error": "unknown turn id"}
    if row["state"] != "done":
        return 200, {"status": _STATUS[row["state"]]}
    if row.get("error"):
        return 200, {"status": "error", "error": row["error"]}
    return 200, {"status": "done", "reply": row.get("reply") or "",
                 "merged": bool(row.get("merged"))}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict):
        out = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        if self.path != "/turn":
            self.send_error(404)
            return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            env = json.loads(body)
            wait = env.pop("wait", False)
            for k in ("channel", "sender", "tier", "text"):
                if not isinstance(env.get(k), str):
                    return self._json(400, {"error": f"missing field: {k}"})
            if env["tier"] == "agent" or env["tier"] not in (
                    "owner", "family", "unknown"):
                return self._json(400, {"error": "tier must be owner|family|unknown"})
            row_id, dup = brain.submit(env)
            if wait:
                code, view = _view(brain.wait(row_id))
                if view.get("status") == "error":
                    return self._json(500, {"error": view["error"]})
                return self._json(200, {"reply": view.get("reply", "")})
            self._json(202, {"id": f"m{row_id}", "duplicate": dup})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_GET(self):
        if not self.path.startswith("/turn/"):
            self.send_error(404)
            return
        tid = self.path.rsplit("/", 1)[1]
        try:
            row = brain.store.get(int(tid.lstrip("m")))
        except ValueError:
            row = None
        code, view = _view(row)
        self._json(code, view)

    def log_message(self, *a):
        pass


def timer_loop():
    while True:
        time.sleep(10)
        for t in db.due_timers():
            brain.submit(tools.timer_envelope(t, cfg["language"]))
            db.finish_timer(t["id"])


def main():
    brain.start()
    threading.Thread(target=timer_loop, daemon=True).start()
    if config.enabled(cfg, "comms"):
        # enabled-but-broken fails loud: the service exits and systemd shows why
        from .comms_v1 import start
        from .loop import run_turn
        start(run_turn, cfg, db)
        print(f"[comms] attached as {cfg['comms']['alias']}", flush=True)
    print(f"[brain] {cfg['assistant_name']} on {cfg['model']}, listening on "
          f"{cfg['host']}:{cfg['http_port']}", flush=True)
    ThreadingHTTPServer((cfg["host"], cfg["http_port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
