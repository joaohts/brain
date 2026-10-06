"""Offline tests: config loading/validation and tool gating. No network.

    .venv/bin/python -m unittest discover tests
"""

import os
import pathlib
import stat
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


def load(text: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
    try:
        return config.load(f.name)
    finally:
        os.unlink(f.name)


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_with_everything_off(self):
        cfg = config.load(config.REPO / "config.example.toml")
        self.assertEqual(cfg["model"], "gpt-6-luna")
        self.assertEqual(cfg["reasoning_effort"], "medium")
        for name in config.INTEGRATIONS:
            self.assertFalse(config.enabled(cfg, name), name)

    def test_section_keys_map_and_paths_resolve(self):
        cfg = load('[model]\nname = "m"\n[server]\nport = 4999\n'
                   '[agent]\nowner_name = "Ada"\n')
        self.assertEqual((cfg["model"], cfg["http_port"], cfg["owner_name"]),
                         ("m", 4999, "Ada"))
        self.assertTrue(os.path.isabs(cfg["db_path"]))

    def test_unknown_keys_and_sections_fail(self):
        with self.assertRaises(config.ConfigError):
            load("[agent]\nownr_name = 'x'\n")
        with self.assertRaises(config.ConfigError):
            load("[voice]\nenabled = true\n")

    def test_enabled_but_broken_integrations_fail_loud(self):
        with self.assertRaises(config.ConfigError):
            load('[whatsapp]\nenabled = true\ncontacts_file = "/nonexistent.json"\n')
        with self.assertRaises(config.ConfigError):
            load('[comms]\nenabled = true\nsocket = "/nonexistent.sock"\n')
        with self.assertRaises(config.ConfigError):
            load('[calendar]\nenabled = true\nscript = ""\n')

    def test_disabled_integrations_are_not_validated(self):
        cfg = load('[calendar]\nenabled = false\nscript = "/nonexistent"\n')
        self.assertFalse(config.enabled(cfg, "calendar"))


class ToolGatingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(config.REPO / "config.example.toml")

    def names(self, tier, cfg=None):
        return {t["name"] for t in tools.schema_for(tier, cfg or self.cfg)}

    def test_disabled_integrations_expose_no_tools(self):
        owner = self.names("owner")
        self.assertIn("remember", owner)
        for gated in ("claude_spawn", "comms_who", "calendar_read"):
            self.assertNotIn(gated, owner)

    def test_tier_rings(self):
        self.assertEqual(self.names("unknown"), {"send_to"})
        self.assertNotIn("remember", self.names("family"))
        self.assertIn("remember", self.names("owner"))

    def test_calendar_tools_appear_and_format_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            script = pathlib.Path(d, "cal")
            script.write_text("#!/bin/sh\necho '[]'\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            cfg = load(f'[agent]\nowner_name = "Ada"\ntimezone = "Europe/Lisbon"\n'
                       f'[calendar]\nenabled = true\nscript = "{script}"\n')
            schema = {t["name"]: t for t in tools.schema_for("owner", cfg)}
            self.assertIn("Ada", schema["calendar_read"]["description"])
            self.assertIn("Europe/Lisbon", schema["calendar_read"]["description"])
            env = {"turn_id": "t", "channel": "cli", "sender": "x", "tier": "owner"}
            self.assertEqual(tools.execute("calendar_read", env,
                                           {"action": "today"}, cfg, FakeDB()), "[]")

    def test_execute_denies_below_ring_and_logs(self):
        db = FakeDB()
        env = {"turn_id": "t", "channel": "wpp:x", "sender": "x", "tier": "unknown"}
        self.assertEqual(tools.execute("remember", env, {"fact": "f"}, self.cfg, db),
                         "denied by policy")
        self.assertEqual(db.steps[0][0][1], "policy_denial")

    def test_execute_refuses_tools_of_disabled_integrations(self):
        env = {"turn_id": "t", "channel": "cli", "sender": "x", "tier": "owner"}
        self.assertEqual(tools.execute("claude_spawn", env, {"task": "x"},
                                       self.cfg, FakeDB()),
                         "unknown tool: claude_spawn")

    def test_deliver_refuses_disabled_channels(self):
        out = tools.deliver("wpp:alice", "hi", "test", self.cfg, FakeDB())
        self.assertTrue(out.startswith("unknown or disabled channel"))
        self.assertEqual(tools.deliver("cli", "hi", "test", self.cfg, FakeDB()),
                         "delivered on cli")

    def test_session_id_spellings(self):
        for raw in ("brain-1a2b", "session-brain-1a2b", "host:session-brain-1a2b",
                    "comms:host:session-brain-1a2b"):
            self.assertEqual(tools._norm_sid(raw), "brain-1a2b", raw)


if __name__ == "__main__":
    unittest.main()
