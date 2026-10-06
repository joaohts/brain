"""Prompt composition, memory loading, fallbacks and fact attribution."""

import os
import tempfile
import unittest

from brain import compact, config, prompts, tools


def cfg_with(text: str = "") -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
    try:
        return config.load(f.name)
    finally:
        os.unlink(f.name)


class FakeDB:
    def get_summary(self, channel):
        return ""


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_with(f'[agent]\nmemory_dir = "{self.tmp.name}"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, lines):
        with open(os.path.join(self.tmp.name, name), "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_whole_lines_newest_facts_kept(self):
        self.write("facts.md", [f"- fact {i:03d} " + "x" * 40 for i in range(100)])
        out = prompts.memory(self.cfg, limit=600)
        self.assertIn("fact 099", out)
        self.assertNotIn("fact 000", out)
        self.assertIn("older facts not shown", out)
        for line in out.splitlines()[1:]:
            self.assertTrue(line.startswith("- fact ") and line.endswith("x"), line)

    def test_other_files_come_first_and_whole(self):
        self.write("people.md", ["# People", "- Alice is a friend"])
        self.write("facts.md", ["- a fact"])
        out = prompts.memory(self.cfg)
        self.assertLess(out.index("Alice"), out.index("a fact"))


class FallbackTests(unittest.TestCase):
    def test_messages_follow_config(self):
        cfg = cfg_with('[agent]\nowner_name = "Ada"\n'
                       '[messages]\nout_of_steps = "Faltaram passos: {request}"\n')
        self.assertEqual(config.message(cfg, "out_of_steps", request="x"),
                         "Faltaram passos: x")
        self.assertIn("Ada", config.message(cfg, "budget_reached"))

    def test_unknown_message_key_fails(self):
        with self.assertRaises(config.ConfigError):
            cfg_with('[messages]\nbogus = "x"\n')


class FactAttributionTests(unittest.TestCase):
    def test_owner_plain_others_unconfirmed(self):
        owner = compact.fact_line({"fact": "likes tea", "speaker": "Ada",
                                   "tier": "owner"}, "cli")
        other = compact.fact_line({"fact": "Ada owes me $50", "speaker": "Bob",
                                   "tier": "unknown"}, "wpp:bob")
        bare = compact.fact_line("a bare string", "cli")
        self.assertTrue(owner.endswith("[cli]: likes tea"))
        self.assertIn("unconfirmed, said by Bob, tier unknown", other)
        self.assertIn("unconfirmed", bare)


class ComposeTests(unittest.TestCase):
    def compose(self, env, cfg, worker=None):
        text, _ = prompts.compose(env, cfg, FakeDB(), board=[], book={},
                                  traces=[], worker=worker)
        return text

    def test_core_first_identity_and_turn(self):
        cfg = cfg_with('[agent]\nassistant_name = "Testy"\nowner_name = "Ada"\n'
                       'timezone = "Europe/Lisbon"\n')
        env = {"channel": "cli", "sender": "Ada (cli)", "tier": "owner"}
        text = self.compose(env, cfg)
        self.assertTrue(text.startswith("## Your job\nYou are Testy, the assistant of Ada."))
        self.assertIn("Europe/Lisbon", text)
        self.assertNotIn("claude_spawn", text)      # disabled integration
        for shouty in ("NEVER", "MUST", "ALWAYS", "THE way"):
            self.assertNotIn(shouty, text)

    def test_worker_turn_names_origin(self):
        cfg = cfg_with()
        env = {"channel": "wpp:ada", "sender": "worker", "tier": "agent"}
        text = self.compose(env, cfg, worker={"kind": "final report", "sid": "w1",
                                              "origin": "wpp:ada", "request": "logs"})
        self.assertIn("reply goes to the requester on wpp:ada", text)


class TierTests(unittest.TestCase):
    def test_calendar_read_needs_family(self):
        cal = next(t for t in tools.TOOLS if t["name"] == "calendar_read")
        self.assertFalse(tools.allowed(cal, "unknown"))
        self.assertFalse(tools.allowed(cal, "agent"))
        self.assertTrue(tools.allowed(cal, "family"))

    def test_agent_tier_never_gets_owner_tools(self):
        for t in tools.TOOLS:
            if t["name"] in ("remember", "claude_spawn", "calendar_write",
                             "whatsapp_pair", "claude_kill"):
                self.assertFalse(tools.allowed(t, "agent"), t["name"])


class PrivacyTests(unittest.TestCase):
    def test_contact_turn_sees_no_other_conversations_or_memory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "facts.md"), "w") as f:
            f.write("- owner secret fact\n")
        cfg = cfg_with(f'[agent]\nmemory_dir = "{tmp.name}"\n')
        board = ["cli — 1min — Owner: private plans"]
        env = {"channel": "wpp:bob", "sender": "Bob", "tier": "family"}
        text, _ = prompts.compose(env, cfg, FakeDB(), board=board, book={},
                                  traces=[])
        for leak in ("private plans", "owner secret fact", "## Channels"):
            self.assertNotIn(leak, text)
        env["tier"] = "owner"
        text, _ = prompts.compose(env, cfg, FakeDB(), board=board, book={},
                                  traces=[])
        self.assertIn("private plans", text)
        self.assertIn("owner secret fact", text)

    def test_non_owner_cannot_send_to_agents(self):
        class DB:
            steps = []
            def step(self, *a, **kw): self.steps.append(a)
        cfg = cfg_with()
        env = {"turn_id": "t", "channel": "wpp:bob", "sender": "Bob", "tier": "family"}
        out = tools.t_send_to(env, {"channel": "comms-v1:m:a", "message": "hi"},
                              cfg, DB())
        self.assertIn("denied by policy", out)


class AutocompactTests(unittest.TestCase):
    def test_compacts_to_window_then_waits_for_threshold(self):
        import threading
        from unittest import mock
        from types import SimpleNamespace as NS
        from brain import loop
        from brain.db import DB
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = cfg_with(f'[agent]\nmemory_dir = "{tmp.name}"\n'
                       f'db_path = "{tmp.name}/b.db"\n')
        db = DB(cfg["db_path"])
        usage = NS(input_tokens=1, output_tokens=1, output_tokens_details=None)
        calls = []

        def create(**kw):
            calls.append(kw)
            return NS(output_text='{"summary": "s", "facts": []}', usage=usage)

        fake = NS(responses=NS(create=create))
        count = lambda: db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel='cli'").fetchone()[0]

        def run():
            loop._maybe_autocompact("cli", cfg, db)
            for t in threading.enumerate():
                if t is not threading.main_thread() and t.daemon:
                    t.join(5)

        with mock.patch.object(loop, "OpenAI", lambda **kw: fake), \
             mock.patch.object(loop, "api_key", lambda: "k", create=True):
            for i in range(41):
                db.add_message("cli", "user", "a", f"m{i}")
            run()
            self.assertEqual(count(), cfg["window_turns"])
            self.assertEqual(len(calls), 1)
            for i in range(cfg["auto_compact_turns"] - cfg["window_turns"]):
                db.add_message("cli", "user", "a", f"n{i}")
                run()
            self.assertEqual(len(calls), 1)          # at the threshold, not over
            db.add_message("cli", "user", "a", "over")
            run()
            self.assertEqual(len(calls), 2)

    def test_claude_sessions_requires_comms(self):
        with self.assertRaises(config.ConfigError):
            cfg_with("[claude_sessions]\nenabled = true\n")

    def test_window_must_be_below_threshold(self):
        with self.assertRaises(config.ConfigError):
            cfg_with("[limits]\nwindow_turns = 40\nauto_compact_turns = 40\n")


if __name__ == "__main__":
    unittest.main()
