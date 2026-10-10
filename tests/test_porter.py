import threading
import unittest
from unittest import mock

from brain import inbox


class PorterReportTest(unittest.TestCase):
    def popen_args(self, fn, *a, **kw):
        with mock.patch("subprocess.Popen") as p:
            fn(*a, **kw)
        self.assertEqual(p.call_count, 1)
        return p.call_args[0][0]

    def test_every_event_is_kept(self):
        args = self.popen_args(inbox._porter, "stop", "Last answered x")
        self.assertIn("--keep", args)
        self.assertEqual(args[args.index("--kind") + 1], "stop")
        self.assertNotIn("--heartbeat", args)

    def test_heartbeat_args(self):
        for paused, want in ((False, "false"), (True, "true")):
            args = self.popen_args(inbox._porter_heartbeat, paused)
            self.assertEqual(args[args.index("--kind") + 1], "update")
            self.assertIn("--keep", args)
            self.assertIn("--heartbeat", args)
            self.assertEqual(args[args.index("--paused") + 1], want)
            self.assertNotIn("--at", args)
            for flag, val in (("--agent", "joana"), ("--harness", "joana"),
                              ("--title", "Joana"), ("--project", "brain")):
                self.assertEqual(args[args.index(flag) + 1], val)

    def test_paused_follows_budget_reached(self):
        b = inbox.Brain.__new__(inbox.Brain)
        b.cfg, b.db = {}, None
        with mock.patch("brain.budget.reached", return_value={"period": "daily"}):
            self.assertTrue(b.porter_paused())
        with mock.patch("brain.budget.reached", return_value=None):
            self.assertFalse(b.porter_paused())
        with mock.patch("brain.budget.reached", side_effect=RuntimeError("db")):
            self.assertFalse(b.porter_paused())

    def test_heartbeat_thread_pings_and_stops(self):
        b = inbox.Brain.__new__(inbox.Brain)
        b._porter_hb, b._stop = None, threading.Event()
        beats = threading.Semaphore(0)
        b.porter_beat = lambda: beats.release()
        b.start_porter_heartbeat(every=0.01)
        b.start_porter_heartbeat(every=0.01)   # idempotent
        for _ in range(3):
            self.assertTrue(beats.acquire(timeout=2))
        b._stop.set()
        b._porter_hb.join(2)
        self.assertFalse(b._porter_hb.is_alive())

    def test_failed_spawn_never_raises(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("no comms")):
            inbox._porter_heartbeat(False)
            inbox._porter("prompt")


if __name__ == "__main__":
    unittest.main()
