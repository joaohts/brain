"""HTTP door — submit-and-poll, so no client ever blocks on the brain's lock.

  POST /turn {channel, sender, tier, text}        -> 202 {"id": t}
  POST /turn {..., "wait": true}                  -> 200 {"reply": ...}  (blocking, for curl/tests)
  GET  /turn/<id>                                 -> {"status": "pending"}
                                                     {"status": "done", "reply": ...}

The brain serializes globally (one thought at a time) and a turn may take
minutes; results are held in memory until fetched (or ~30 min). Also runs the
timer loop: due timers become self-envelopes."""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .db import DB
from .loop import run_turn

cfg = config.load()
db = DB(cfg["db_path"])

_results: dict[str, dict] = {}
_results_guard = threading.Lock()
RESULT_TTL = 1800


def _submit(env: dict) -> str:
    tid = f"turn_{uuid.uuid4().hex[:10]}"
    with _results_guard:
        _results[tid] = {"status": "pending", "ts": time.time()}

    def job():
        def mark_running():
            with _results_guard:
                _results[tid] = {"status": "running", "ts": time.time()}
        try:
            reply = run_turn(env, cfg, db, on_start=mark_running)
            status = {"status": "done", "reply": reply}
        except Exception as e:
            status = {"status": "error", "error": str(e)}
        with _results_guard:
            _results[tid] = {**status, "ts": time.time()}

    threading.Thread(target=job, daemon=True).start()
    return tid


def _gc_results():
    with _results_guard:
        dead = [k for k, v in _results.items()
                if time.time() - v["ts"] > RESULT_TTL]
        for k in dead:
            del _results[k]


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
            if env.pop("wait", False):
                self._json(200, {"reply": run_turn(env, cfg, db)})
            else:
                self._json(202, {"id": _submit(env)})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_GET(self):
        if not self.path.startswith("/turn/"):
            self.send_error(404)
            return
        _gc_results()
        tid = self.path.rsplit("/", 1)[1]
        with _results_guard:
            r = _results.get(tid)
        if r is None:
            self._json(404, {"error": "unknown turn id"})
        else:
            self._json(200, {k: v for k, v in r.items() if k != "ts"})

    def log_message(self, *a):
        pass


def timer_loop():
    while True:
        time.sleep(10)
        for t in db.due_timers():
            db.finish_timer(t["id"])
            _submit(dict(channel=t["channel"], sender="timer (internal)",
                         tier="owner",
                         text=f"[TIMER FIRED] Deliver this reminder now, "
                              f"in {cfg['language']}, on this channel: "
                              f"{t['message']}"))


def main():
    threading.Thread(target=timer_loop, daemon=True).start()
    if config.enabled(cfg, "comms"):
        # enabled-but-broken fails loud: the service exits and systemd shows why
        from .comms_v1 import start
        start(run_turn, cfg, db)
        print(f"[comms] attached as {cfg['comms']['alias']}", flush=True)
    print(f"[brain] {cfg['assistant_name']} on {cfg['model']}, listening on "
          f"{cfg['host']}:{cfg['http_port']}", flush=True)
    ThreadingHTTPServer((cfg["host"], cfg["http_port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
