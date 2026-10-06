"""whatsapp_pair tool and [whatsapp] owner_channel config. Fakes only: no
sidecar, no network, no real sleeping.

    .venv/bin/python -m unittest discover tests
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from brain import config, tools


class FakeDB:
    def __init__(self):
        self.steps, self.messages = [], []

    def step(self, *a, **kw):
        self.steps.append((a, kw))

    def add_message(self, *a):
        self.messages.append(a)


class FakeSidecar:
    """Scripted sidecar: a list of QR strings it rotates through, and the poll
    number at which the phone scans (None = never)."""

    def __init__(self, qrs, connect_after=None, repair_status=200):
        self.qrs, self.connect_after = qrs, connect_after
        self.repair_status = repair_status
        self.polls, self.calls = 0, []

    def http(self, cfg, method, path, timeout=10):
        self.calls.append((method, path))
        if path == "/repair":
            return self.repair_status, {"ok": True, "moved_to": "wa/auth.old-x"}
        if path == "/status":
            self.polls += 1
            if self.connect_after is not None and self.polls > self.connect_after:
                return 200, {"connected": True, "jid": "100:1@s.whatsapp.net",
                             "loggedOut": False}
            return 200, {"connected": False, "jid": None, "loggedOut": False}
        if path == "/qr":
            qr = self.qrs[min((self.polls - 1) // 2, len(self.qrs) - 1)]
            return 200, {"qr": qr, "text": f"<render {qr}>"}
        return 404, {}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def wa_cfg(tmp, **extra):
    contacts = os.path.join(tmp, "allow.json")
    with open(contacts, "w") as f:
        json.dump({}, f)
    lines = [f'[whatsapp]\nenabled = true\ncontacts_file = "{contacts}"']
    lines += [f"{k} = {json.dumps(v)}" for k, v in extra.items()]
    path = os.path.join(tmp, "c.toml")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return config.load(path)


class WhatsappPairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = wa_cfg(self.tmp.name)
        self.clock = Clock()
        self.saved = dict(tools._wa)

    def tearDown(self):
        tools._wa.clear()
        tools._wa.update(self.saved)
        self.tmp.cleanup()

    def run_pair(self, sidecar, channel="comms-v1:m_x:a_y", tier="owner"):
        tools._wa.update(http=sidecar.http, sleep=self.clock.sleep, clock=self.clock)
        db = FakeDB()
        delivered = []
        orig = tools.deliver
        tools.deliver = lambda ch, msg, origin, cfg, db_: (
            delivered.append((ch, msg)) or "queued")
        try:
            env = {"turn_id": "t1", "channel": channel, "sender": "s", "tier": tier}
            with contextlib.redirect_stdout(io.StringIO()):
                out = tools.execute("whatsapp_pair", env, {}, self.cfg, db)
        finally:
            tools.deliver = orig
        return out, delivered, db

    def test_owner_only(self):
        for tier in ("unknown", "family"):
            side = FakeSidecar(["Q1"], connect_after=1)
            out, delivered, db = self.run_pair(side, tier=tier)
            self.assertEqual(out, "denied by policy")
            self.assertEqual(side.calls, [])          # never touched the sidecar
            self.assertEqual(db.steps[0][0][1], "policy_denial")
            self.assertNotIn("whatsapp_pair",
                             {t["name"] for t in tools.schema_for(tier, self.cfg)})
        self.assertIn("whatsapp_pair",
                      {t["name"] for t in tools.schema_for("owner", self.cfg)})

    def test_qr_codes_go_only_to_the_requesting_channel(self):
        side = FakeSidecar(["Q1", "Q2", "Q3"], connect_after=6)
        out, delivered, db = self.run_pair(side, channel="comms-v1:m_x:a_y")
        self.assertEqual(out, "connected as 100:1@s.whatsapp.net")
        self.assertEqual({ch for ch, _ in delivered}, {"comms-v1:m_x:a_y"})
        # each distinct QR once, in order, as the rendered text
        self.assertEqual([m.split("\n", 1)[1] for _, m in delivered],
                         ["<render Q1>", "<render Q2>", "<render Q3>"])
        self.assertEqual(side.calls[0], ("POST", "/repair"))

    def test_refused_from_whatsapp(self):
        side = FakeSidecar(["Q1"])
        out, delivered, _ = self.run_pair(side, channel="wpp:owner")
        self.assertTrue(out.startswith("refused"))
        self.assertEqual((delivered, side.calls), ([], []))

    def test_timeout(self):
        side = FakeSidecar(["Q1", "Q2"], connect_after=None)
        out, delivered, db = self.run_pair(side)
        self.assertTrue(out.startswith("timed out"), out)
        self.assertGreaterEqual(self.clock.t, tools.WA_PAIR_TIMEOUT)
        self.assertLess(self.clock.t, tools.WA_PAIR_TIMEOUT + tools.WA_PAIR_POLL + 1)
        self.assertEqual(len(delivered), 2)
        self.assertIn("whatsapp_pair_timeout", [a[1] for a, _ in db.steps])

    def test_already_connected_after_repair(self):
        side = FakeSidecar(["Q1"], connect_after=0)
        out, delivered, _ = self.run_pair(side)
        self.assertEqual(out, "connected as 100:1@s.whatsapp.net")
        self.assertEqual(delivered, [])

    def test_sidecar_down_fails_plainly(self):
        side = FakeSidecar(["Q1"], repair_status=0)
        out, delivered, _ = self.run_pair(side)
        self.assertTrue(out.startswith("whatsapp pairing FAILED"), out)
        self.assertEqual(delivered, [])

    def test_disabled_whatsapp_hides_the_tool(self):
        cfg = config.load(config.REPO / "config.example.toml")
        self.assertNotIn("whatsapp_pair",
                         {t["name"] for t in tools.schema_for("owner", cfg)})


class OwnerChannelConfigTests(unittest.TestCase):
    def test_default_and_valid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(wa_cfg(tmp)["whatsapp"]["owner_channel"], "cli")

    def test_rejects_whatsapp_and_unknown_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("wpp:owner", "voice:home", ""):
                with self.assertRaises(config.ConfigError, msg=bad):
                    wa_cfg(tmp, owner_channel=bad)

    def test_comms_owner_channel_needs_comms(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                wa_cfg(tmp, owner_channel="comms-v1:m_a:a_b")

    def test_shell_exports_owner_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = config._shell(wa_cfg(tmp), "whatsapp")
        self.assertIn("export WA_OWNER_CHANNEL=cli", out)


if __name__ == "__main__":
    unittest.main()
