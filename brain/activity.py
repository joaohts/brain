"""Claude session activity, for the inactivity timeout.

Every Claude Code process on the host is a session here, whoever started it.
Its activity comes from Claude Code hooks: scripts/claude-activity-hook.sh,
registered in ~/.claude/settings.json, appends each event (prompt, tool
call start and end, subagent, compaction, stop, permission request) to
<state>/activity/<session_id>.log with the claude pid and its /proc
starttime. The transcript's mtime counts too (Claude writes it as it works,
including when no hook fires, e.g. streaming a reply). Looking at either is
not activity.

A tool whose PreToolUse has no PostToolUse yet is in flight: the session is
busy, not idle, however long the tool runs, up to TOOL_CAP (then it is taken
for hung).

A session that has fired no hook yet (it predates the hook and has been
idle since; running sessions do pick it up) is "legacy": its activity is
the transcript's mtime and Claude's own status file
(~/.claude/sessions/<pid>.json, status busy counts like a tool in flight),
and the clock never starts before the reaper first saw it, so the
migration itself never ends one.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from dataclasses import dataclass, field

TOOL_CAP = 24 * 3600   # a tool in flight longer than this is taken for hung


@dataclass
class Session:
    sid: str                      # Claude session id ("" for unknown legacy)
    pid: int
    start: str                    # /proc starttime of pid
    tmux: str = ""                # tmux session name, if inside tmux
    transcript: str = ""
    last: float = 0.0             # latest activity, epoch seconds
    pending_since: float | None = None   # oldest tool in flight
    legacy: bool = False
    sources: list = field(default_factory=list)

    def idle_for(self, now: float) -> float:
        return now - self.last

    def busy(self, now: float) -> bool:
        return (self.pending_since is not None
                and now - self.pending_since < TOOL_CAP)


def state_dir(cfg) -> str:
    return os.path.expanduser(cfg.get("claude_sessions", {}).get("state_dir")
                              or "~/.local/state/claude-sessions")


def proc_start(pid: int, proc: str = "/proc") -> str | None:
    """starttime of pid if it is a live claude process, else None."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    comm = stat[stat.find("(") + 1:stat.rfind(")")]
    rest = stat[stat.rfind(")") + 2:].split()
    if comm != "claude" or len(rest) < 20:
        return None
    return rest[19]


def claude_pids(proc: str = "/proc") -> dict[int, str]:
    """{pid: starttime} of every live claude process."""
    out = {}
    for name in os.listdir(proc):
        if name.isdigit():
            start = proc_start(int(name), proc)
            if start:
                out[int(name)] = start
    return out


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def read_log(path: str) -> Session | None:
    """The session a hook log describes (None if empty or unreadable)."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    sid = os.path.basename(path)[:-len(".log")]
    s, pending = None, {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        ts, event, detail, transcript, pid, start, tmux = parts[:7]
        if not pid.isdigit():
            continue
        t = int(ts) / 1000
        if s is None or (int(pid), start) != (s.pid, s.start):
            s, pending = Session(sid, int(pid), start), {}
        s.last = max(s.last, t)
        s.transcript = transcript or s.transcript
        s.tmux = tmux or s.tmux
        if event == "PreToolUse":
            pending[detail or f"#{len(pending)}"] = t
        elif event in ("PostToolUse", "PostToolUseFailure"):
            if detail in pending:
                del pending[detail]
            elif not detail and pending:
                pending.pop(next(iter(pending)))
        elif event in ("Stop", "SessionStart", "StopFailure"):
            pending = {}
    if s is None:
        return None
    s.pending_since = min(pending.values()) if pending else None
    s.sources.append("hooks")
    tm = _mtime(s.transcript)
    if tm > s.last:
        s.last = tm
        s.sources.append("transcript")
    return s


def _registry(pid: int, start: str, claude_home: str) -> dict:
    try:
        with open(os.path.join(claude_home, "sessions", f"{pid}.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    # procStart is the same /proc starttime: a reused pid never matches
    if str(d.get("procStart", start)) != start:
        return {}
    return d


def sessions(cfg, now: float | None = None, proc: str = "/proc",
             claude_home: str | None = None) -> list[Session]:
    """Every live claude process, with its activity. Logs of dead processes
    are removed once a day old; so are legacy first-seen marks."""
    now = time.time() if now is None else now
    claude_home = claude_home or os.path.expanduser("~/.claude")
    root = state_dir(cfg)
    live = claude_pids(proc)
    found: dict[tuple, Session] = {}
    for path in glob.glob(os.path.join(root, "activity", "*.log")):
        s = read_log(path)
        if s and live.get(s.pid) == s.start:
            key = (s.pid, s.start)
            if key not in found or s.last > found[key].last:
                found[key] = s
        elif now - _mtime(path) > 86400:
            _remove(path)
    seen_dir = os.path.join(root, "legacy")
    os.makedirs(seen_dir, exist_ok=True)
    for pid, start in live.items():
        if (pid, start) in found:
            continue
        mark = os.path.join(seen_dir, f"{pid}-{start}")
        if not os.path.exists(mark):
            with open(mark, "w") as f:
                f.write(str(now))
        try:
            with open(mark) as f:
                first = float(f.read().strip() or now)
        except (OSError, ValueError):
            first = now
        reg = _registry(pid, start, claude_home)
        s = Session(reg.get("sessionId", ""), pid, start, legacy=True,
                    last=first, sources=["first seen"])
        s.tmux = (reg.get("tmux") or "").split(":", 1)[0] or tmux_of(pid, proc)
        if s.sid:
            hits = glob.glob(os.path.join(claude_home, "projects", "*",
                                          f"{s.sid}.jsonl"))
            s.transcript = hits[0] if hits else ""
        tm = _mtime(s.transcript)
        if tm > s.last:
            s.last, s.sources = tm, s.sources + ["transcript"]
        if reg.get("status") == "busy":
            s.pending_since = float(reg.get("statusUpdatedAt", now * 1000)) / 1000
        found[(pid, start)] = s
    for mark in glob.glob(os.path.join(seen_dir, "[0-9]*-*")):
        pid, _, start = os.path.basename(mark).partition("-")
        if pid.isdigit() and live.get(int(pid)) != start:
            _remove(mark)
    return list(found.values())


def still_idle(s: Session, cfg, cutoff: float, now: float,
               proc: str = "/proc", claude_home: str | None = None) -> bool:
    """Re-read just before ending it: same process, still inactive."""
    if proc_start(s.pid, proc) != s.start:
        return False
    for t in sessions(cfg, now, proc=proc, claude_home=claude_home):
        if (t.pid, t.start) == (s.pid, s.start):
            return t.last <= cutoff and not t.busy(now)
    return False


def tmux_of(pid: int, proc: str = "/proc") -> str:
    """tmux session a process runs in (from its TMUX_PANE), or ""."""
    try:
        with open(f"{proc}/{pid}/environ", "rb") as f:
            env = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0")
                       if b"=" in kv)
    except OSError:
        return ""
    pane = env.get(b"TMUX_PANE", b"").decode()
    if not pane:
        return ""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane,
                            "#{session_name}"], capture_output=True,
                           text=True, timeout=10)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass
