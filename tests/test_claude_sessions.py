"""scripts/claude-sessions.sh keeps the tmux server out of the unit that
started it, so restarting the brain (KillMode=control-group) leaves managed
sessions running. Needs tmux and a systemd user manager; skipped otherwise.
Uses a private tmux socket and throwaway transient units, never the real
brain.service or the default tmux server.

    .venv/bin/python -m unittest tests.test_claude_sessions
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from brain import config

SCRIPT = str(config.REPO / "scripts" / "claude-sessions.sh")


def _user_systemd() -> bool:
    if not (shutil.which("tmux") and shutil.which("systemd-run")
            and shutil.which("busctl")):
        return False
    return subprocess.run(["systemctl", "--user", "show-environment"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(_user_systemd(), "needs tmux and a systemd user manager")
class TmuxServerIsolation(unittest.TestCase):
    def setUp(self):
        # tmux socket paths are limited to ~100 bytes: keep the dir short
        self.tmp = tempfile.mkdtemp(prefix="cst.", dir="/tmp")
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.env["TMUX_TMPDIR"] = self.tmp
        self.env["CLAUDE_SESSIONS_LOG_DIR"] = self.tmp
        self.units = []

    def tearDown(self):
        self.tmux("kill-server")
        for u in self.units:
            subprocess.run(["systemctl", "--user", "stop", u], capture_output=True)
            subprocess.run(["systemctl", "--user", "reset-failed", u],
                           capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], capture_output=True, text=True,
                              env=self.env)

    def fake_brain(self, shell: str) -> str:
        """A service like brain.service (control-group KillMode) running
        `shell` with the script sourced, then staying up."""
        unit = f"cst-brain-{uuid.uuid4().hex[:8]}.service"
        self.units.append(unit)
        cmd = ["systemd-run", "--user", "--quiet", f"--unit={unit}",
               "-p", "KillMode=control-group", f"--setenv=TMUX_TMPDIR={self.tmp}",
               f"--setenv=CLAUDE_SESSIONS_LOG_DIR={self.tmp}",
               "--", "bash", "-c",
               f"source {SCRIPT}; {shell}; exec sleep infinity"]
        subprocess.run(cmd, check=True, env=self.env)
        return unit

    def wait_session(self, name):
        for _ in range(50):
            if self.tmux("has-session", "-t", name).returncode == 0:
                return
            time.sleep(0.1)
        self.fail(f"tmux session {name} never came up")

    def server_unit(self) -> str:
        pid = self.tmux("display-message", "-p", "#{pid}").stdout.strip()
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read().strip().rsplit("/", 1)[-1]

    def stop(self, unit):
        subprocess.run(["systemctl", "--user", "stop", unit], check=True)
        time.sleep(0.5)

    def test_control_plain_server_dies_with_its_unit(self):
        """The bug: a server forked inside the unit goes when it stops."""
        unit = self.fake_brain("tmux new-session -d -s w0")
        self.wait_session("w0")
        self.assertEqual(self.server_unit(), unit)
        self.stop(unit)
        self.assertNotEqual(self.tmux("has-session", "-t", "w0").returncode, 0)

    def test_new_server_starts_in_its_own_scope(self):
        unit = self.fake_brain("new_session -d -s w1; "
                               f"tmux pipe-pane -t w1 'cat >> {self.tmp}/w1.log'")
        self.wait_session("w1")
        self.assertRegex(self.server_unit(), r"^claude-tmux-.*\.scope$")
        self.stop(unit)
        self.assertEqual(self.tmux("has-session", "-t", "w1").returncode, 0)
        # the pane's logger survived too
        self.tmux("send-keys", "-t", "w1", "echo still-here", "Enter")
        time.sleep(0.5)
        with open(f"{self.tmp}/w1.log") as f:
            self.assertIn("still-here", f.read())

    def test_existing_server_is_adopted(self):
        unit = self.fake_brain("tmux new-session -d -s w2; "
                               f"tmux pipe-pane -t w2 'cat >> {self.tmp}/w2.log'")
        self.wait_session("w2")
        self.assertEqual(self.server_unit(), unit)
        r = subprocess.run(["bash", SCRIPT, "isolate"], capture_output=True,
                           text=True, env={**self.env,
                                           "CLAUDE_SESSIONS_ADOPT_FROM": unit})
        self.assertIn("adopted", r.stdout)
        self.assertRegex(self.server_unit(), r"^claude-tmux-.*\.scope$")
        self.stop(unit)
        self.assertEqual(self.tmux("has-session", "-t", "w2").returncode, 0)
        self.tmux("send-keys", "-t", "w2", "echo adopted-log", "Enter")
        time.sleep(0.5)
        with open(f"{self.tmp}/w2.log") as f:
            self.assertIn("adopted-log", f.read())
        # idempotent
        r = subprocess.run(["bash", SCRIPT, "isolate"], capture_output=True,
                           text=True, env=self.env)
        self.assertIn("already isolated", r.stdout)

    def test_other_units_are_left_alone(self):
        unit = self.fake_brain("tmux new-session -d -s w3")
        self.wait_session("w3")
        r = subprocess.run(["bash", SCRIPT, "isolate"], capture_output=True,
                           text=True, env={**self.env,
                                           "CLAUDE_SESSIONS_ADOPT_FROM": "x.service"})
        self.assertIn("left alone", r.stdout)
        self.assertEqual(self.server_unit(), unit)

    def test_scope_goes_when_server_exits(self):
        self.fake_brain("new_session -d -s w4")
        self.wait_session("w4")
        scope = self.server_unit()
        self.tmux("kill-server")
        time.sleep(0.5)
        r = subprocess.run(["systemctl", "--user", "is-active", scope],
                           capture_output=True, text=True)
        self.assertNotEqual(r.stdout.strip(), "active")


if __name__ == "__main__":
    unittest.main()
