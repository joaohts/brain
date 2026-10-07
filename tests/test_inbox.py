"""Inbox, turn lease, step-boundary merging, worker routing and the agent
tier. Offline: a scripted fake stands in for the model client.

    .venv/bin/python -m unittest discover tests
"""

import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS

from brain import config, inbox, tools
from brain.db import DB


def usage():
    return NS(input_tokens=10, output_tokens=5,
              output_tokens_details=NS(reasoning_tokens=0))


def text(t):
    return NS(output=[NS(type="message")], output_text=t, usage=usage(),
              status="completed")


def call(name, args, cid="c1"):
    return NS(output=[NS(type="function_call", name=name,
                         arguments=json.dumps(args), call_id=cid)],
              output_text="", usage=usage(), status="completed")


class FakeClient:
    """script: list of (response, hook) — hook runs while the model "thinks"."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.responses = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        resp, hook = self.script.pop(0) if self.script else (text("(extra)"), None)
        if hook:
            hook()
        return resp

    def inputs(self, i):
        return " ".join(str(m.get("content", "")) if isinstance(m, dict) else ""
                        for m in self.calls[i]["input"])

    def tool_names(self, i):
        return {t["name"] for t in (self.calls[i]["tools"] or [])}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.load(config.REPO / "config.example.toml")
        cfg["db_path"] = os.path.join(self.tmp.name, "brain.db")
        cfg["memory_dir"] = os.path.join(self.tmp.name, "memory")
        cfg["identity_file"] = os.path.join(self.tmp.name, "identity.md")
        cfg["daily_budget_usd"] = 0
        self.cfg = cfg
        self.db = DB(cfg["db_path"])
        self.client = FakeClient([])
        self.brain = inbox.Brain(cfg, self.db, client_factory=lambda: self.client,
                                 lease_seconds=60)
        self.killed = []
        self._saved = (tools.kill_session, tools.managed_source,
                       tools.tmux_sessions)
        tools.kill_session = self.fake_kill
        # the host, faked: claude processes under proc/, tmux sessions and
        # their CLAUDE_SESSION_SOURCE stamps, hook logs under state/
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(self.proc)
        self.brain.proc_root = self.proc
        self.brain.claude_home = os.path.join(self.tmp.name, "claude")
        cfg["claude_sessions"]["state_dir"] = os.path.join(self.tmp.name, "state")
        cfg["claude_sessions"]["reap_scope"] = "managed"
        self.tmux, self.sources, self.pids = set(), {}, {}
        tools.managed_source = lambda name: self.sources.get(name)
        from brain import activity
        self._tmux_of = activity.tmux_of
        activity.tmux_of = lambda pid, proc="/proc": self.pane_of.get(pid, "")
        self.pane_of = {}
        tools.tmux_sessions = lambda: None if self.tmux is None else set(self.tmux)

    def tearDown(self):
        (tools.kill_session, tools.managed_source,
         tools.tmux_sessions) = self._saved
        from brain import activity
        activity.tmux_of = self._tmux_of
        self.tmp.cleanup()

    def fake_kill(self, cfg, sid):
        """claude-sessions.sh kill: the tmux session and its claude go."""
        self.killed.append(sid)
        self.tmux.discard(sid)
        pid = self.pids.pop(sid, None)
        if pid:
            import shutil
            shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)
        return "killed"

    def claude(self, tmux="", source="brain", sid="", at=None, start="100"):
        """A live claude process (optionally inside a tmux session); with
        sid, its hook log starts with SessionStart at `at`."""
        pid = 1000 + len(os.listdir(self.proc))
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        fields = ["S", "1"] + ["0"] * 17 + [start, "0"]
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} (claude) " + " ".join(fields))
        if tmux:
            self.tmux.add(tmux)
            self.pids[tmux] = pid
            self.pane_of[pid] = tmux
            if source:
                self.sources[tmux] = source
        if sid:
            self.hook(sid, "SessionStart", time.time() if at is None else at,
                      pid=pid, start=start, tmux=tmux)
        return pid

    def hook(self, sid, event, at, detail="", pid=None, start="100", tmux="",
             transcript=""):
        d = os.path.join(self.cfg["claude_sessions"]["state_dir"], "activity")
        os.makedirs(d, exist_ok=True)
        if pid is None:   # continue the session's own process
            with open(os.path.join(d, f"{sid}.log")) as f:
                last = f.read().splitlines()[-1].split("\t")
            pid, start, tmux = int(last[4]), last[5], last[6]
        with open(os.path.join(d, f"{sid}.log"), "a") as f:
            f.write("\t".join([str(int(at * 1000)), event, detail, transcript,
                               str(pid), start, tmux]) + "\n")

    def env(self, text_, sender="Owner (cli)", tier="owner", channel="cli", **kw):
        return dict(channel=channel, sender=sender, tier=tier, text=text_, **kw)

    def submit(self, *a, **kw):
        return self.brain.submit(self.env(*a, **kw), kick=False)[0]

    def steps(self, name):
        return [r for r in self.db.conn.execute(
            "SELECT payload FROM steps WHERE step=?", (name,))]

    def worker(self, sid="w1", origin="cli", recipient="m_x:a_w"):
        self.brain.store.add_worker(sid, origin, "", "Owner (cli)", "summarize the logs")
        self.brain.store.set_worker(sid, recipient=recipient, state="running")
        w = self.brain.store.worker(sid)
        self.claude(tmux=sid, sid=f"cs-{sid}", at=w["created_at"])
        return w

    def submit_worker(self, body, w=None, pid="comms:m_x:msg1"):
        w = w or self.brain.store.worker("w1")
        env, meta = inbox.worker_result_envelope(w, "worker session-w1", body, pid)
        return self.brain.submit(env, route="deliver", meta=meta,
                                 kind="worker_result", kick=False)[0]

    def wait_reaped(self):
        for _ in range(50):
            if self.killed:
                return
            time.sleep(0.02)


class MergeTests(Base):
    def test_message_during_tool_call_is_merged(self):
        head = self.submit("look at the thread")
        follow = []
        self.client.script = [
            (call("read_thread", {"channel": "cli"}),
             lambda: follow.append(self.submit("also check yesterday"))),
            (text("done, both covered"), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("also check yesterday", self.client.inputs(1))
        self.assertIn("tier owner", self.client.inputs(1))
        h, f = self.brain.store.get(head), self.brain.store.get(follow[0])
        self.assertEqual(h["reply"], "done, both covered")
        self.assertEqual((f["state"], f["merged"], f["reply"]), ("done", 1, ""))
        self.assertEqual(f["turn_id"], h["turn_id"])

    def test_message_during_final_answer_discards_draft(self):
        head = self.submit("what time is the meeting")
        self.client.script = [
            (text("draft answer"), lambda: self.submit("the one on Friday")),
            (text("Friday's meeting is at 10"), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("the one on Friday", self.client.inputs(1))
        self.assertEqual(self.brain.store.get(head)["reply"],
                         "Friday's meeting is at 10")
        self.assertEqual(len(self.steps("draft_discarded")), 1)

    def test_draft_discard_gets_a_step_even_at_the_cap(self):
        self.cfg["max_tool_steps"] = 1
        head = self.submit("q")
        self.client.script = [(text("draft"), lambda: self.submit("more")),
                              (text("final"), None)]
        self.brain.drain()
        self.assertEqual(self.brain.store.get(head)["reply"], "final")

    def test_different_sender_is_not_merged_and_gets_own_turn_at_own_tier(self):
        head = self.submit("owner asks")
        other = []
        self.client.script = [
            (text("owner answer"),
             lambda: other.append(self.submit("hi, Bob here", sender="Bob",
                                              tier="family"))),
            (text("hello Bob"), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)
        self.assertNotIn("Bob here", self.client.inputs(0))
        self.assertIn("hi, Bob here", self.client.inputs(1))
        self.assertIn("remember", self.client.tool_names(0))
        self.assertNotIn("remember", self.client.tool_names(1))   # family tier
        self.assertEqual(self.brain.store.get(head)["reply"], "owner answer")
        b = self.brain.store.get(other[0])
        self.assertEqual((b["reply"], b["merged"]), ("hello Bob", 0))

    def test_other_thread_of_same_sender_is_not_merged(self):
        self.submit("in thread a", thread="a")
        self.client.script = [(text("a"), lambda: self.submit("in b", thread="b")),
                              (text("b"), None)]
        self.brain.drain()
        self.assertNotIn("in b", self.client.inputs(0))
        self.assertEqual(len(self.client.calls), 2)

    def test_merge_caps(self):
        head = self.submit("head")
        self.brain.store.mark_read([head], "t1", "h")
        for i in range(25):
            self.submit(f"m{i}")
        got = self.brain.store.pull("cli", "", "Owner (cli)", "t1", "h")
        self.assertEqual(len(got), inbox.MERGE_MAX_MSGS)
        got = self.brain.store.pull("cli", "", "Owner (cli)", "t1", "h")
        self.assertEqual(len(got), 5)

    def test_merge_char_cap_never_starves(self):
        for _ in range(3):
            self.submit("x" * 3000)
        got = self.brain.store.pull("cli", "", "Owner (cli)", "t1", "h")
        self.assertEqual(len(got), 1)            # 3000 + 3000 > 4000
        big = self.submit("y" * 9000, channel="wpp:z")
        got = self.brain.store.pull("wpp:z", "", "Owner (cli)", "t2", "h")
        self.assertEqual([r["id"] for r in got], [big])   # at least one row

    def test_rows_arriving_while_idle_all_get_answered(self):
        a = self.submit("first")
        b = self.submit("second", sender="Bob", tier="family")
        self.client.script = [(text("r1"), None), (text("r2"), None)]
        self.brain.drain()
        self.assertEqual(self.brain.store.get(a)["reply"], "r1")
        self.assertEqual(self.brain.store.get(b)["reply"], "r2")
        self.assertIsNone(self.brain.store.lease()["holder"])


class IntakeTests(Base):
    def test_duplicate_provider_id_is_dropped(self):
        a, dup_a = self.brain.submit(self.env("hi", provider_id="wa:ABC"), kick=False)
        b, dup_b = self.brain.submit(self.env("hi", provider_id="wa:ABC"), kick=False)
        self.assertEqual((a, dup_a, dup_b), (b, False, True))
        self.client.script = [(text("once"), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(self.steps("inbox_duplicate")), 1)

    def test_run_turn_still_returns_the_reply(self):
        from brain.loop import run_turn
        self.client.script = [(text("sync reply"), None)]
        inbox._current = self.brain
        self.assertEqual(run_turn(self.env("hi"), self.cfg, self.db,
                                  client=self.client), "sync reply")

    def test_failed_turn_still_completes_its_rows(self):
        row = self.submit("boom")

        def raise_():
            raise RuntimeError("model down")
        self.client.script = [(text("x"), raise_)]
        self.brain.drain()
        r = self.brain.store.get(row)
        self.assertEqual(r["state"], "done")
        self.assertIn("model down", r["error"])


class LeaseTests(Base):
    def test_held_lease_blocks_and_expired_lease_is_reclaimable(self):
        other = f"{os.getpid()}:other"                  # alive holder
        self.assertTrue(self.brain.store.claim_lease(other, 60))
        self.assertFalse(self.brain.store.claim_lease(self.brain.holder, 60))
        self.brain.store.conn.execute("UPDATE turn_lease SET expires_at=0")
        self.assertTrue(self.brain.store.claim_lease(self.brain.holder, 60))

    def test_a_crashed_holders_rows_return_to_unread(self):
        row = self.submit("lost?")
        dead = "999999999:crashed"
        self.brain.store.claim_lease(dead, 60)
        self.brain.store.mark_read([row], "t_dead", dead)
        self.brain.store.conn.execute("UPDATE turn_lease SET expires_at=0")
        self.assertTrue(self.brain.store.claim_lease(self.brain.holder, 60))
        self.assertEqual(self.brain.store.get(row)["state"], "unread")

    def test_dispatch_waits_while_another_process_holds_the_lease(self):
        self.submit("queued")
        self.brain.store.claim_lease(f"{os.getpid()}:other", 60)
        self.brain.drain()
        self.assertEqual(self.client.calls, [])
        self.brain.store.release_lease(f"{os.getpid()}:other")
        self.client.script = [(text("now"), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 1)

    def test_recover_resets_rows_of_dead_processes(self):
        row = self.submit("x")
        self.brain.store.mark_read([row], "t", "999999999:gone")
        self.brain.store.recover(self.brain.prefix)
        self.assertEqual(self.brain.store.get(row)["state"], "unread")


class WorkerTests(Base):
    def test_worker_result_while_idle_wakes_a_turn_and_is_delivered(self):
        self.worker()
        row = self.submit_worker("[FINAL] 3 errors found")
        self.client.script = [(text("The logs show 3 errors."), None)]
        self.brain.drain()
        self.assertIn("[worker report, final, session w1]", self.client.inputs(0))
        self.assertIn("task delegated for cli", self.client.calls[0]["instructions"])
        self.assertIn("reply goes to the requester on cli", self.client.calls[0]["instructions"])
        self.assertEqual(self.client.tool_names(0), {"send_to"})    # agent tier
        r = self.brain.store.get(row)
        self.assertEqual((r["channel"], r["tier"], r["reply"]),
                         ("cli", "agent", "The logs show 3 errors."))
        self.assertEqual(len(self.steps("origin_delivery")), 1)
        self.assertIdleThenReaped()

    def test_progress_result_is_delivered_but_not_reaped(self):
        self.worker()
        self.submit_worker("halfway there")
        self.client.script = [(text("Halfway."), None)]
        self.brain.drain()
        time.sleep(0.05)
        self.assertEqual(self.killed, [])

    def test_worker_result_mid_turn_in_origin_thread_is_merged(self):
        self.worker()
        head = self.submit("is it done yet?")
        wrow = []
        self.client.script = [
            (text("still running"),
             lambda: wrow.append(self.submit_worker("[FINAL] all good"))),
            (text("Yes: all good."), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("all good", self.client.inputs(1))
        self.assertEqual(self.brain.store.get(head)["reply"], "Yes: all good.")
        w = self.brain.store.get(wrow[0])
        self.assertEqual((w["merged"], w["reply"]), (1, ""))
        self.assertIdleThenReaped()

    IDLE = 8 * 3600

    def assertIdleThenReaped(self, sid="w1"):
        """A final result marks the worker idle; it stays routable and is
        reaped only once it has been inactive for idle_minutes (8 h)."""
        w = self.brain.store.worker(sid)
        self.assertEqual(w["state"], "idle")
        self.assertAlmostEqual(w["last_active"], time.time(), delta=30)
        self.assertEqual(self.brain.store.worker_by_recipient(w["recipient"])["sid"], sid)
        self.assertNotReaped(w["last_active"] + self.IDLE - 1)
        self.brain.reap_idle(now=w["last_active"] + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, [sid])
        self.assertEqual(self.brain.store.worker(sid)["state"], "reaped")

    def assertNotReaped(self, now):
        self.brain.reap_idle(now=now)
        time.sleep(0.05)
        self.assertEqual(self.killed, [])

    def test_idle_minutes_zero_reaps_at_once(self):
        self.cfg["claude_sessions"]["idle_minutes"] = 0
        self.worker()
        self.submit_worker("[FINAL] done")
        self.client.script = [(text("Done."), None)]
        self.brain.drain()
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")

    def test_follow_up_wakes_an_idle_worker_and_final_rearms_it(self):
        self.worker()
        self.submit_worker("[FINAL] done", pid="comms:m_x:m1")
        self.client.script = [(text("Done."), None)]
        self.brain.drain()
        first = self.brain.store.worker("w1")["last_active"]
        # the worker took a follow-up: its progress report wakes it
        time.sleep(0.01)
        self.submit_worker("looking into the follow-up", pid="comms:m_x:m2")
        self.client.script = [(text("On it."), None)]
        self.brain.drain()
        w = self.brain.store.worker("w1")
        self.assertEqual(w["state"], "running")
        self.assertGreater(w["last_active"], first)
        self.assertNotReaped(first + self.IDLE)
        # its next final report marks it idle again, with fresh activity
        time.sleep(0.01)
        self.submit_worker("[FINAL] follow-up answered", pid="comms:m_x:m3")
        self.client.script = [(text("Answered."), None)]
        self.brain.drain()
        w = self.brain.store.worker("w1")
        self.assertEqual(w["state"], "idle")
        self.assertIdleThenReaped()

    def test_claude_kill_ends_an_idle_worker_at_once(self):
        self.worker()
        self.brain.park("w1")
        out = tools.t_claude_kill({}, {"session_id": "session-w1"}, self.cfg, self.db)
        self.assertEqual(out, "killed")
        self.assertEqual(self.killed, ["w1"])
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")
        self.assertIsNone(self.brain.store.worker_by_recipient("m_x:a_w"))
        self.brain.reap_idle(now=time.time() + 24 * 3600)
        time.sleep(0.05)
        self.assertEqual(self.killed, ["w1"])

    def followup(self, target="comms-v1:m_x:a_w", ok=True):
        """send_to a worker over comms, the node stubbed."""
        from brain import comms_v1
        saved = comms_v1.deliver
        sent = []

        def fake(t, m):
            sent.append((t, self.brain.store.worker("w1")["state"]))
            return ({"ok": True, "id": "msg", "state": "queued"} if ok
                    else {"ok": False, "error": "comms-v1: node_unavailable"})
        comms_v1.deliver = fake
        self.cfg["comms"]["enabled"] = True
        try:
            out = tools.execute("send_to", {"channel": "cli", "tier": "owner",
                                            "sender": "Owner", "turn_id": "t"},
                                {"channel": target, "message": "and also?"},
                                self.cfg, self.db)
        finally:
            comms_v1.deliver = saved
        return out, sent

    def final(self, body="[FINAL] done", pid="comms:m_x:f1"):
        self.submit_worker(body, pid=pid)
        self.client.script = [(text("ok"), None)]
        self.brain.drain()

    def assertBusy(self, sid="w1"):
        """Running, and not reaped while active (here: within 8 h)."""
        w = self.brain.store.worker(sid)
        self.assertEqual((w["state"], w["idle_until"]), ("running", None))
        self.assertNotReaped(w["last_active"] + self.IDLE - 1)

    def test_follow_up_wakes_the_worker_before_the_send_near_its_deadline(self):
        self.worker()
        self.final()
        before = self.brain.store.worker("w1")["last_active"]
        time.sleep(0.01)
        out, sent = self.followup()
        self.assertEqual(sent, [("m_x:a_w", "running")])   # woken before the send
        self.assertIn("worker session-w1 is awake", out)
        # the follow-up is activity: the clock restarts from it
        self.assertGreater(self.brain.store.worker("w1")["last_active"], before)
        self.assertNotReaped(before + self.IDLE + 0.005)
        self.assertBusy()
        # its next [FINAL] is activity too, then it is reaped once inactive
        self.final("[FINAL] follow-up answered", pid="comms:m_x:f2")
        self.assertIdleThenReaped()

    def test_follow_up_by_address_wakes_the_worker(self):
        self.worker()
        self.final()
        self.followup(target="comms-v1:pi:session-w1")
        self.assertBusy()

    def test_follow_up_to_a_running_worker_outlives_its_crossing_final(self):
        """The worker's [FINAL] for the task crossed the follow-up: it is
        processed after the follow-up went out, so it must not park it."""
        self.worker()
        self.submit_worker("[FINAL] done", pid="comms:m_x:f1")   # not yet handled
        self.followup()
        self.client.script = [(text("ok"), None)]
        self.brain.drain()
        self.assertBusy()
        self.final("[FINAL] follow-up answered", pid="comms:m_x:f2")
        self.assertIdleThenReaped()

    def test_follow_up_sent_in_the_turn_that_merges_the_final(self):
        self.worker()
        self.submit("anything from the worker?")
        self.client.script = [
            (call("send_to", {"channel": "comms-v1:m_x:a_w", "message": "x"}),
             lambda: self.submit_worker("[FINAL] done", pid="comms:m_x:f1")),
            (text("asked it more"), None)]
        from brain import comms_v1
        saved, comms_v1.deliver = comms_v1.deliver, lambda t, m: {
            "ok": True, "id": "msg", "state": "queued"}
        self.cfg["comms"]["enabled"] = True
        try:
            self.brain.drain()
        finally:
            comms_v1.deliver = saved
        self.assertEqual(self.brain.store.get(1)["reply"], "asked it more")
        self.assertBusy()

    def test_failed_follow_up_send_leaves_the_worker_idle(self):
        self.worker()
        self.final()
        before = self.brain.store.worker("w1")["last_active"]
        time.sleep(0.01)
        out, _ = self.followup(ok=False)
        self.assertIn("node_unavailable", out)
        w = self.brain.store.worker("w1")   # undone, its activity too
        self.assertEqual((w["state"], w["last_active"], w["pending"]),
                         ("idle", before, 0))

    def test_reaper_that_listed_a_worker_loses_to_a_follow_up(self):
        self.worker()
        self.final()
        cutoff = self.brain.store.worker("w1")["last_active"] + 0.001
        self.assertEqual(self.brain.store.inactive(cutoff), ["w1"])
        time.sleep(0.01)
        self.followup()
        self.assertFalse(self.brain.store.claim_reap("w1", cutoff))
        self.assertBusy()

    def test_follow_up_to_a_reaped_worker_is_just_sent(self):
        self.worker()
        self.brain.reap("w1")
        out, _ = self.followup()
        self.assertNotIn("awake", out)
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")

    def test_claude_kill_ends_a_busy_worker_at_once(self):
        self.worker()
        self.followup()
        tools.t_claude_kill({}, {"session_id": "session-w1"}, self.cfg, self.db)
        self.assertEqual(self.killed, ["w1"])
        self.final()   # a late [FINAL] doesn't resurrect it
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")

    # -- inactivity timeout --------------------------------------------------

    def test_worker_that_never_sends_final_is_reaped_once_inactive(self):
        w = self.worker()
        self.assertEqual(w["state"], "running")
        self.assertNotReaped(w["created_at"] + self.IDLE - 1)
        self.brain.reap_idle(now=w["created_at"] + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")
        self.assertEqual(len(self.steps("worker_reaped")), 1)

    def test_hook_activity_keeps_a_long_task_alive(self):
        """Not an execution limit: tool calls 10 h after the spawn (no
        report, no [FINAL]) keep it; 8 h after the last one it is reaped."""
        w = self.worker()
        t0 = w["created_at"]
        self.hook("cs-w1", "PreToolUse", t0 + 10 * 3600 - 5, "toolu_1")
        self.hook("cs-w1", "PostToolUse", t0 + 10 * 3600, "toolu_1")
        self.assertNotReaped(t0 + 10 * 3600 + self.IDLE - 1)
        self.brain.reap_idle(now=t0 + 10 * 3600 + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])

    def test_a_tool_in_flight_is_protected_until_the_cap(self):
        from brain import activity
        w = self.worker()
        t0 = w["created_at"]
        self.hook("cs-w1", "PreToolUse", t0 + 60, "toolu_long")
        self.assertNotReaped(t0 + 60 + self.IDLE + 3600)
        self.brain.reap_idle(now=t0 + 60 + activity.TOOL_CAP + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])

    def test_a_finished_tool_no_longer_protects(self):
        w = self.worker()
        t0 = w["created_at"]
        self.hook("cs-w1", "PreToolUse", t0 + 1, "a")
        self.hook("cs-w1", "PreToolUse", t0 + 2, "b")
        self.hook("cs-w1", "PostToolUseFailure", t0 + 3, "a")
        self.hook("cs-w1", "PostToolUse", t0 + 4, "b")
        self.brain.reap_idle(now=t0 + 4 + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])

    def test_transcript_writes_count_as_activity(self):
        w = self.worker()
        t0 = w["created_at"]
        tr = self.transcript(t0 + 5 * 3600)
        self.hook("cs-w1", "UserPromptSubmit", t0 + 1, transcript=tr)
        self.assertNotReaped(t0 + 5 * 3600 + self.IDLE - 1)

    def test_a_transcript_touched_without_new_entries_is_not_activity(self):
        """An idle Claude rewrites its transcript's metadata about hourly;
        only a newer timestamped entry counts."""
        w = self.worker()
        t0 = w["created_at"]
        tr = self.transcript(t0 + 1, mtime=t0 + 5 * 3600)
        self.hook("cs-w1", "UserPromptSubmit", t0 + 1, transcript=tr)
        self.brain.reap_idle(now=t0 + 1 + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])

    def transcript(self, last, mtime=None):
        from datetime import datetime, timezone
        tr = os.path.join(self.tmp.name, "t.jsonl")
        with open(tr, "w") as f:
            f.write("partial line\n")
            for t in (last - 60, last):
                ts = datetime.fromtimestamp(t, timezone.utc).isoformat()
                f.write(json.dumps({"type": "assistant", "timestamp":
                                    ts.replace("+00:00", "Z")}) + "\n")
            f.write(json.dumps({"type": "bridge-session"}) + "\n")
        mtime = last if mtime is None else mtime
        os.utime(tr, (mtime, mtime))
        return tr

    def test_a_report_counts_as_activity_on_arrival(self):
        w = self.worker()
        self.brain.store.set_worker("w1", last_active=w["created_at"] - 3600)
        from brain import comms_v1
        ad = comms_v1.BrainComms.__new__(comms_v1.BrainComms)
        ad.brain, ad.trusted = self.brain, set()
        ad._envelope({"message_json": json.dumps({"body": "still going"}),
                      "sender_machine_id": "m_x", "sender_agent_id": "a_w",
                      "message_id": "m9"})
        self.assertAlmostEqual(self.brain.store.worker("w1")["last_active"],
                               time.time(), delta=5)

    def test_legacy_session_gets_its_full_period_from_migration(self):
        """A claude that started before the hook was registered fires no
        hooks: its clock starts when the reaper first sees it."""
        now = time.time()
        self.claude(tmux="mcp-old", source="mcp")       # no hook log
        self.assertNotReaped(now)                       # first seen: now
        self.assertNotReaped(now + self.IDLE - 60)
        self.brain.reap_idle(now=now + self.IDLE + 60)
        self.wait_reaped()
        self.assertEqual(self.killed, ["mcp-old"])

    def test_legacy_session_busy_in_claudes_status_file_is_protected(self):
        now = time.time()
        pid = self.claude(tmux="", source="")
        reg = os.path.join(self.brain.claude_home, "sessions")
        os.makedirs(reg)
        with open(os.path.join(reg, f"{pid}.json"), "w") as f:
            json.dump({"pid": pid, "procStart": "100", "sessionId": "s-old",
                       "tmux": "mcp-busy:@1.%1", "status": "busy",
                       "statusUpdatedAt": now * 1000}, f)
        self.sources["mcp-busy"] = "mcp"
        self.assertNotReaped(now)
        self.assertNotReaped(now + self.IDLE + 60)

    def test_unmanaged_sessions_are_left_alone_in_managed_scope(self):
        now = time.time()
        self.claude(tmux="personal", source="", sid="cs-p", at=now)
        self.claude(sid="cs-term", at=now)              # a plain terminal
        from unittest import mock
        with mock.patch.object(os, "kill") as kill:
            self.assertNotReaped(now + 10 * self.IDLE)
            kill.assert_not_called()

    def test_all_scope_ends_any_idle_claude_with_term_then_kill(self):
        from unittest import mock
        self.cfg["claude_sessions"]["reap_scope"] = "all"
        now = time.time()
        pid = self.claude(sid="cs-term", at=now)        # a plain terminal
        busy = self.claude(sid="cs-busy", at=now)
        self.hook("cs-busy", "PreToolUse", now + 1, "t")
        with mock.patch.object(os, "kill") as kill:
            self.brain.reap_idle(now=now + self.IDLE - 1)
            kill.assert_not_called()
            self.brain.reap_idle(now=now + self.IDLE + 1)
            kill.assert_called_once_with(pid, 15)
            self.brain.reap_idle(now=now + self.IDLE + 40)   # still there
            kill.assert_called_with(pid, 9)
        self.assertNotIn(busy, [c.args[0] for c in kill.call_args_list])
        self.assertEqual(self.killed, [])               # its terminal stays

    def test_a_reused_pid_is_not_the_old_session(self):
        now = time.time()
        pid = self.claude(tmux="mcp-x", source="mcp", start="200")
        self.hook("cs-gone", "SessionStart", now - 10 * self.IDLE, pid=pid,
                  start="100", tmux="mcp-x")            # older process, same pid
        self.assertNotReaped(now)                       # new one: legacy, fresh

    def test_claude_exited_but_its_managed_tmux_remains(self):
        now = time.time()
        self.tmux.add("mcp-shell")
        self.sources["mcp-shell"] = "mcp"
        self.tmux.add("personal-shell")                 # unmanaged: never
        self.assertNotReaped(now)
        self.assertNotReaped(now + self.IDLE - 60)
        self.brain.reap_idle(now=now + self.IDLE + 60)
        self.wait_reaped()
        self.assertEqual(self.killed, ["mcp-shell"])

    def test_worker_whose_tmux_is_gone_is_marked_reaped(self):
        w = self.worker()
        self.fake_kill(self.cfg, "w1")
        self.killed.clear()
        self.brain.reap_idle(now=w["created_at"] + 601)
        self.assertEqual(self.brain.store.worker("w1")["state"], "reaped")
        self.assertEqual(self.killed, [])

    def test_unreadable_tmux_reaps_no_claudeless_session(self):
        w = self.worker()
        self.fake_kill(self.cfg, "w1")
        self.killed.clear()
        self.tmux = None
        self.brain.reap_idle(now=w["created_at"] + 10 * self.IDLE)
        self.assertEqual(self.brain.store.worker("w1")["state"], "running")

    def test_a_worker_is_killed_once_per_sweep(self):
        w = self.worker()
        self.brain.reap_idle(now=w["created_at"] + self.IDLE + 1)
        time.sleep(0.1)
        self.assertEqual(self.killed, ["w1"])

    def test_idle_minutes_zero_still_times_out_unfinished_workers(self):
        self.cfg["claude_sessions"]["idle_minutes"] = 0
        w = self.worker()
        self.assertNotReaped(w["created_at"] + self.IDLE - 1)
        self.brain.reap_idle(now=w["created_at"] + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["w1"])

    def test_brain_scope_only_times_out_our_own(self):
        self.cfg["claude_sessions"]["reap_scope"] = "brain"
        self.cfg["claude_sessions"]["source"] = "brain"
        now = time.time()
        self.claude(tmux="mcp-1", source="mcp", sid="cs-m", at=now)
        self.claude(tmux="brain-1", source="brain", sid="cs-b", at=now)
        self.brain.reap_idle(now=now + self.IDLE + 1)
        self.wait_reaped()
        self.assertEqual(self.killed, ["brain-1"])

    def test_the_hook_records_events_and_skips_idle_nudges(self):
        import subprocess
        hook = os.path.join(config.REPO, "scripts", "claude-activity-hook.sh")
        state = os.path.join(self.tmp.name, "hookstate")
        env = dict(os.environ, CLAUDE_SESSIONS_STATE_DIR=state)
        env.pop("TMUX_PANE", None)

        def fire(**payload):
            subprocess.run(["bash", hook], input=json.dumps(payload),
                           text=True, env=env, check=True, timeout=10)
        log = os.path.join(state, "activity", "s1.log")
        fire(hook_event_name="SessionStart", session_id="s1")
        fire(hook_event_name="PreToolUse", session_id="s1", tool_use_id="t1",
             transcript_path="/x/s1.jsonl")
        fire(hook_event_name="Notification", session_id="s1",
             notification_type="idle_prompt")
        fire(hook_event_name="Notification", session_id="s1",
             notification_type="permission_prompt")
        fire(hook_event_name="SessionEnd", session_id="s1")
        fire(hook_event_name="PreToolUse", session_id="../evil")
        rows = [l.split("\t") for l in open(log).read().splitlines()]
        self.assertEqual([(r[1], r[2], r[3]) for r in rows],
                         [("SessionStart", "", ""),
                          ("PreToolUse", "t1", "/x/s1.jsonl"),
                          ("Notification", "permission_prompt", "")])
        self.assertTrue(all(len(r) == 7 for r in rows))
        fire(hook_event_name="Stop", session_id="s1")   # starts over
        self.assertEqual(len(open(log).read().splitlines()), 1)
        self.assertEqual(os.listdir(os.path.join(state, "activity")), ["s1.log"])

    def test_old_workers_table_is_migrated(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE workers (sid TEXT PRIMARY KEY, recipient TEXT, "
                  "origin_channel TEXT NOT NULL, origin_thread TEXT NOT NULL "
                  "DEFAULT '', origin_sender TEXT NOT NULL, request TEXT NOT "
                  "NULL, state TEXT NOT NULL, created_at REAL NOT NULL)")
        c.execute("INSERT INTO workers VALUES('old','r','cli','','o','q','running',0)")
        c.commit(); c.close()
        store = inbox.Store(path)
        self.assertIsNone(store.worker("old")["idle_until"])
        self.assertEqual(store.worker("old")["pending"], 1)   # owes its [FINAL]
        self.assertIsNone(store.worker("old")["last_active"])
        # unobserved activity counts from its spawn; observed, it moves on
        self.assertEqual(store.inactive(1.0), ["old"])
        store.touch_worker("old", 5.0)
        self.assertEqual(store.inactive(1.0), [])
        store.touch_worker("old", 3.0)          # never backwards
        self.assertEqual(store.worker("old")["last_active"], 5.0)

    def test_worker_result_waits_for_another_senders_turn_then_handed_off(self):
        self.worker(origin="cli")
        peer = self.submit("peer question", sender="peer agent",
                           tier="unknown", channel="comms-v1:m_p:a_p")
        wrow = []
        self.client.script = [
            (text("peer answer"),
             lambda: wrow.append(self.submit_worker("[FINAL] result"))),
            (text("Delivered result."), None)]
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)
        self.assertNotIn("result", self.client.inputs(0).replace("peer question", ""))
        self.assertEqual(self.brain.store.get(peer)["reply"], "peer answer")
        w = self.brain.store.get(wrow[0])
        self.assertEqual((w["channel"], w["tier"], w["merged"], w["reply"]),
                         ("cli", "agent", 0, "Delivered result."))

    def test_agent_tier_send_to_only_reaches_origin(self):
        env = {"channel": "cli", "origin": "cli", "tier": "agent",
               "sender": "worker", "turn_id": "t"}
        self.assertTrue(tools.execute("send_to", env, {"channel": "wpp:alice",
                                                       "message": "x"},
                                      self.cfg, self.db).startswith("denied"))
        self.assertEqual(tools.execute("send_to", env, {"channel": "cli",
                                                        "message": "x"},
                                       self.cfg, self.db), "delivered on cli")
        self.assertEqual(tools.execute("remember", env, {"fact": "f"},
                                       self.cfg, self.db), "denied by policy")
        self.assertEqual({t["name"] for t in tools.schema_for("agent", self.cfg)},
                         {"send_to"})


class SpawnTests(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: getattr(tools, k) for k in (
            "_create_session", "_find_peer", "_post_task", "_pane_problem",
            "SPAWN_POLL_SECONDS")}
        tools.SPAWN_POLL_SECONDS = 0
        tools._pane_problem = lambda sid, settled: ""
        self.posted = []
        tools._post_task = lambda cfg, r, sid, task: self.posted.append((r, task)) or ""
        tools._find_peer = lambda sid: "m_pi:a_worker"
        tools._create_session = lambda cfg, cwd: "brain-1a2b"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(tools, k, v)
        super().tearDown()

    def spawn(self):
        env = {"channel": "wpp:owner", "thread": "", "sender": "Owner",
               "tier": "owner", "turn_id": "t_x"}
        before = set(threading.enumerate())
        out = tools.t_claude_spawn(env, {"task": "fix the build"}, self.cfg, self.db)
        for t in set(threading.enumerate()) - before:
            t.join(5)
        return out

    def test_spawn_returns_at_once_and_records_the_origin(self):
        out = self.spawn()
        self.assertIn("asynchronous", out)
        w = self.brain.store.worker_by_recipient("m_pi:a_worker")
        self.assertEqual((w["sid"], w["origin_channel"], w["state"]),
                         ("brain-1a2b", "wpp:owner", "running"))
        self.assertIn(inbox.FINAL_MARK, self.posted[0][1])
        self.assertIn("do NOT close comms or /exit", self.posted[0][1])
        self.assertIn("inactive for 8 hours", self.posted[0][1])

    def test_spawn_with_no_idle_keeps_exit_instruction(self):
        self.cfg["claude_sessions"]["idle_minutes"] = 0
        self.spawn()
        self.assertIn("end this session with /exit", self.posted[0][1])
        self.assertIn("inactive for 8 hours is closed", self.posted[0][1])

    def test_spawn_failure_lands_in_the_origin_thread(self):
        def boom(cfg, cwd):
            raise RuntimeError("Login expired")
        tools._create_session = boom
        self.spawn()
        row = self.brain.store.next_unread(self.brain.routes)
        self.assertEqual((row["channel"], row["kind"], row["tier"]),
                         ("wpp:owner", "system", "agent"))
        self.assertIn("spawn FAILED: Login expired", row["text"])
        self.assertEqual(row["meta"]["deliver_to"], "wpp:owner")


class CommsEnvelopeTests(Base):
    def test_tiers_and_worker_routing(self):
        from brain.comms_v1 import BrainComms
        bc = BrainComms(None, self.cfg, self.db, socket_path="/nonexistent",
                        state_path=os.path.join(self.tmp.name, "adapter.db"),
                        trusted_machines=("m_trusted",))
        self.assertEqual(self.brain.routes["comms"], bc._route)
        self.worker(recipient="m_trusted:a_w")

        def job(machine, agent, body="hi"):
            return {"sender_machine_id": machine, "sender_agent_id": agent,
                    "message_id": "msg1", "message_json": json.dumps({"body": body})}

        env, meta, kind = bc._envelope(job("m_trusted", "a_w", "[FINAL] ok"))
        self.assertEqual((env["channel"], env["tier"], kind, meta["final"]),
                         ("cli", "agent", "worker_result", True))
        env, _, kind = bc._envelope(job("m_trusted", "a_human"))
        self.assertEqual((env["tier"], kind), ("owner", "message"))
        env, _, _ = bc._envelope(job("m_stranger", "a_x"))
        self.assertEqual(env["tier"], "unknown")
        self.assertEqual(env["provider_id"], "comms:m_stranger:msg1")
        bc.journal.close()


if __name__ == "__main__":
    unittest.main()
