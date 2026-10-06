"""comms tools against a real but isolated comms node (its own data dir and
socket in a temp dir; the host's live node is never touched). Skipped when
the comms binary is missing or cannot start a node.

    .venv/bin/python -m unittest tests.test_comms_tools
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from brain import comms_v1, config, tools
from tests.test_brain import FakeDB

COMMS = shutil.which("comms") or os.path.expanduser("~/.local/bin/comms")


def _env(tier="owner", channel="cli"):
    return {"turn_id": "t", "channel": channel, "sender": "x", "tier": tier}


@unittest.skipUnless(os.access(COMMS, os.X_OK), "comms binary not installed")
class CommsToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="brain-comms-test-")
        data = os.path.join(cls.tmp, "node")
        cls.sock = os.path.join(data, "node.sock")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COMMS_", "CLAUDE"))}
        cls.node = subprocess.Popen([COMMS, "--data-dir", data, "serve"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env)
        for _ in range(50):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.1)
        else:
            cls.node.kill()
            raise unittest.SkipTest("isolated comms node did not start")
        cfg_path = os.path.join(cls.tmp, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(f'[agent]\ndb_path = "{cls.tmp}/brain.db"\n'
                    f'[comms]\nenabled = true\nsocket = "{cls.sock}"\n'
                    f'state_path = "{cls.tmp}/adapter.db"\n')
        cls.cfg = config.load(cfg_path)
        comms_v1._active = None
        comms_v1.start(lambda env, cfg, db: "NO_REPLY", cls.cfg, FakeDB())
        # a second, receiver-less identity on the same isolated node
        client = comms_v1.NodeClient(cls.sock)
        cls.peer = client.request("POST", "/v1/sessions", {
            "alias": "peer", "persistent": True, "scope": "local",
            "harness": "service", "harness_session_id": "peer-test",
            "process_id": os.getpid(), "process_started": comms_v1.process_stamp()})
        cls.peer_id = f"{cls.peer['agent']['machine_id']}:{cls.peer['agent']['id']}" \
            if "machine_id" in cls.peer["agent"] else None

    @classmethod
    def tearDownClass(cls):
        if comms_v1._active is not None:
            comms_v1._active.close()
            comms_v1._active = None
        cls.node.terminate()
        cls.node.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_tool(self, name, args=None, tier="owner", channel="cli", db=None):
        return tools.execute(name, _env(tier, channel), args or {}, self.cfg,
                             db or FakeDB())

    def peer_recipient(self):
        rows = json.loads(self.run_tool("comms_who"))
        return next(r["recipient"] for r in rows if r["address"].endswith(":peer"))

    def test_who_lists_exact_recipients(self):
        rows = json.loads(self.run_tool("comms_who"))
        self.assertEqual(set(rows[0]), {"address", "recipient", "persistent", "online"})
        addrs = {r["address"].split(":")[-1] for r in rows}
        self.assertTrue({"brain", "peer"} <= addrs, rows)

    def test_send_to_returns_id_and_state_through_the_single_path(self):
        target = self.peer_recipient()
        with mock.patch.object(comms_v1, "deliver", wraps=comms_v1.deliver) as spy:
            out = self.run_tool("send_to", {"channel": f"comms-v1:{target}",
                                            "message": "ping"})
        spy.assert_called_once()
        self.assertRegex(out, r"id \S+, state \w+")
        msg_id = out.split("id ", 1)[1].split(",")[0]
        status = json.loads(self.run_tool("comms_status", {"message_id": msg_id}))
        self.assertEqual(status["id"], msg_id)
        self.assertIn(status["state"], ("queued", "received", "handed_off"))

    def test_status_of_unknown_message(self):
        self.assertEqual(self.run_tool("comms_status", {"message_id": "msg_nope"}),
                         "comms-v1: not_found")

    def test_log_and_inbox_are_read_only(self):
        target = self.peer_recipient()
        self.run_tool("send_to", {"channel": f"comms-v1:{target}", "message": "logged"})
        log = json.loads(self.run_tool("comms_log", {"peer": target, "limit": 50}))
        self.assertTrue(any(m["body"].startswith("logged")
                            for m in log["messages"]), log)
        first = json.loads(self.run_tool("comms_inbox"))
        again = json.loads(self.run_tool("comms_inbox"))
        self.assertEqual(first, again)  # inspecting never consumes mail
        self.run_tool("send_to", {"channel": f"comms-v1:{target}", "message": "more"})
        page = json.loads(self.run_tool("comms_log", {"peer": target, "limit": 1}))
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(set(page["messages"][0]),
                         {"id", "from", "to", "state", "created_at", "body"})
        nxt = json.loads(self.run_tool("comms_log", {
            "peer": target, "limit": 1, "cursor": page["next_cursor"]}))
        self.assertNotEqual(nxt["messages"][0]["id"], page["messages"][0]["id"])

    def test_provenance_stamp_only_on_real_relays(self):
        target = self.peer_recipient()
        chan = f"comms-v1:{target}"
        self.run_tool("send_to", {"channel": chan, "message": "own words"},
                      channel=chan)
        self.run_tool("send_to", {"channel": chan, "message": "carried"},
                      channel="wpp:alice")
        bodies = [m["body"] for m in json.loads(self.run_tool(
            "comms_log", {"peer": target, "limit": 50}))["messages"]]
        self.assertIn("own words", bodies)
        self.assertTrue(any(b.startswith("carried\n[relayed from wpp:alice")
                            for b in bodies), bodies)

    def test_tier_policy(self):
        for tier in ("unknown", "family"):
            db = FakeDB()
            self.assertEqual(self.run_tool("comms_who", tier=tier, db=db),
                             "denied by policy")
            self.assertEqual(db.steps[0][0][1], "policy_denial")
            names = {t["name"] for t in tools.schema_for(tier, self.cfg)}
            self.assertFalse(any(n.startswith("comms_") for n in names), names)
        agent = {t["name"] for t in tools.schema_for("agent", self.cfg)}
        self.assertTrue({"comms_who", "comms_status", "comms_log",
                         "comms_inbox"} <= agent)
        self.assertNotIn("remember", agent)
        self.assertTrue(self.run_tool("comms_who", tier="agent").startswith("["))

    def test_identity_names_this_node(self):
        r = comms_v1.identity()
        self.assertTrue(r["ok"])
        self.assertTrue(r["result"].endswith(":brain"), r)


if __name__ == "__main__":
    unittest.main()
