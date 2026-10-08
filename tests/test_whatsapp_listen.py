"""whatsapp_listen tool: turning the WhatsApp channel off and on. Fakes only:
no sidecar, no network.

    .venv/bin/python -m unittest discover tests
"""

import tempfile
import unittest

from brain import tools
from tests.test_whatsapp_pair import FakeDB, wa_cfg


class ListenSidecar:
    """The sidecar's /listen and /status, kept in memory."""

    def __init__(self, status=200):
        self.status, self.calls = status, []
        self.state = {"listening": True, "until": None, "connected": True}

    def http(self, cfg, method, path, timeout=10, body=None):
        self.calls.append((method, path, body))
        if self.status != 200:
            return self.status, {"error": "boom"}
        if path == "/listen":
            until = "2026-10-08T14:30:00.000Z" if body.get("minutes") else None
            self.state = {"listening": body["on"],
                          "until": None if body["on"] else until,
                          "connected": body["on"]}
            return 200, {"ok": True, **self.state}
        if path == "/status":
            return 200, dict(self.state)
        return 404, {}


class WhatsappListenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = wa_cfg(self.tmp.name)
        self.cfg["timezone"] = "America/Sao_Paulo"
        self.saved = dict(tools._wa)
        self.side = ListenSidecar()
        tools._wa.update(http=self.side.http)

    def tearDown(self):
        tools._wa.clear()
        tools._wa.update(self.saved)
        self.tmp.cleanup()

    def call(self, args, channel="comms-v1:m_x:a_y", tier="owner"):
        db = FakeDB()
        env = {"turn_id": "t1", "channel": channel, "sender": "s", "tier": tier}
        return tools.execute("whatsapp_listen", env, args, self.cfg, db), db

    def test_owner_only(self):
        for tier in ("unknown", "family"):
            out, db = self.call({"action": "off"}, tier=tier)
            self.assertEqual(out, "denied by policy")
            self.assertNotIn("whatsapp_listen",
                             {t["name"] for t in tools.schema_for(tier, self.cfg)})
        self.assertEqual(self.side.calls, [])
        self.assertIn("whatsapp_listen",
                      {t["name"] for t in tools.schema_for("owner", self.cfg)})

    def test_off_until_turned_on(self):
        out, db = self.call({"action": "off"})
        self.assertEqual(self.side.calls, [("POST", "/listen", {"on": False})])
        self.assertIn("OFF until turned back on", out)
        self.assertIn("comms or the CLI", out)
        self.assertNotIn("timer", out)
        self.assertEqual(db.steps[0][0][1], "whatsapp_listen")

    def test_off_for_minutes_shows_local_time(self):
        out, _ = self.call({"action": "off", "minutes": 90})
        self.assertEqual(self.side.calls[0][2], {"on": False, "minutes": 90})
        self.assertIn("OFF until 2026-10-08 11:30 (America/Sao_Paulo)", out)
        self.assertIn("or by the timer", out)

    def test_off_from_whatsapp_warns_it_is_the_last_reply(self):
        out, _ = self.call({"action": "off"}, channel="wpp:joao")
        self.assertIn("last WhatsApp message", out)

    def test_on_and_status(self):
        self.call({"action": "off"})
        self.assertIn("OFF", self.call({"action": "status"})[0])
        out, _ = self.call({"action": "on"})
        self.assertEqual(out, "WhatsApp listening is ON")
        self.assertEqual(self.side.calls[-1], ("POST", "/listen", {"on": True}))
        self.assertEqual(self.call({"action": "status"})[0], "WhatsApp listening is ON")

    def test_bad_minutes_never_reach_the_sidecar(self):
        for bad in (0, -5, "2h"):
            out, _ = self.call({"action": "off", "minutes": bad})
            self.assertTrue(out.startswith("minutes must be"))
        self.assertEqual(self.side.calls, [])

    def test_sidecar_down(self):
        self.side.status = 0
        out, db = self.call({"action": "off"})
        self.assertTrue(out.startswith("whatsapp_listen FAILED"))
        self.assertEqual(db.steps, [])


if __name__ == "__main__":
    unittest.main()
