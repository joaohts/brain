"""Offline tests for the [vault] tools: tier gating, confinement (.., absolute
paths, symlinks, hidden and denied paths), limits, atomic writes and
concurrent-edit protection. Everything runs in temporary directories.

    .venv/bin/python -m unittest tests.test_vault
"""

import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from brain import config, tools, vault
from tests.test_brain import FakeDB, load


def env(tier="owner"):
    return {"channel": "wpp:joao", "sender": "joao", "tier": tier,
            "turn_id": "t1"}


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "vault"
        self.outside = base / "outside"
        (self.root / "projects").mkdir(parents=True)
        (self.root / "archive").mkdir()
        (self.root / ".obsidian").mkdir()
        (self.root / "private" / "creds").mkdir(parents=True)
        self.outside.mkdir()
        (self.root / "TODO.md").write_text("- [ ] buy milk\n- [ ] call Ana\n")
        (self.root / "projects" / "pi.md").write_text("# Pi\nport 3401\n")
        (self.root / "archive" / "old.md").write_text("frozen\n")
        (self.root / ".obsidian" / "app.md").write_text("cfg\n")
        (self.root / "private" / "creds" / "secrets.md").write_text("SECRET\n")
        (self.root / "image.png").write_bytes(b"\x89PNG")
        (self.outside / "secret.md").write_text("outside secret\n")
        self.cfg = load(f'[vault]\nenabled = true\npath = "{self.root}"\n'
                        f'read_only = ["archive", "templates"]\n'
                        f'deny = ["private/creds/secrets.md", "*/secrets.md"]\n')
        self.v = self.cfg["vault"]
        self.db = FakeDB()

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, tier="owner", **args):
        return tools.execute(name, env(tier), args, self.cfg, self.db)


class GatingTests(VaultTestCase):
    NAMES = {"vault_list", "vault_search", "vault_read", "vault_create",
             "vault_edit"}

    def test_owner_only_in_schema(self):
        owner = {t["name"] for t in tools.schema_for("owner", self.cfg)}
        self.assertTrue(self.NAMES <= owner)
        for tier in ("family", "unknown", tools.AGENT, "bogus"):
            seen = {t["name"] for t in tools.schema_for(tier, self.cfg)}
            self.assertFalse(self.NAMES & seen, tier)

    def test_execution_denied_below_owner(self):
        for tier in ("family", "unknown", tools.AGENT):
            out = self.call("vault_read", tier=tier, path="TODO.md")
            self.assertIn("denied", out)
            out = self.call("vault_create", tier=tier, path="x.md", content="x")
            self.assertIn("denied", out)
        self.assertFalse((self.root / "x.md").exists())

    def test_tier_rechecked_inside_tool(self):
        out = tools.t_vault_read(env("family"), {"path": "TODO.md"},
                                 self.cfg, self.db)
        self.assertIn("owner-only", out)

    def test_disabled_vault_exposes_nothing(self):
        cfg = load("")
        names = {t["name"] for t in tools.schema_for("owner", cfg)}
        self.assertFalse(self.NAMES & names)

    def test_config_validation(self):
        with self.assertRaises(config.ConfigError):
            load('[vault]\nenabled = true\npath = "/nonexistent/vault"\n')
        with self.assertRaises(config.ConfigError):
            load(f'[vault]\nenabled = true\npath = "{self.root}"\n'
                 f'max_results = 0\n')
        with self.assertRaises(config.ConfigError):
            load(f'[vault]\nenabled = true\npath = "{self.root}"\n'
                 f'deny = "x"\n')


class ConfinementTests(VaultTestCase):
    def test_traversal_and_absolute_refused(self):
        for p in ("../outside/secret.md", "projects/../../outside/secret.md",
                  str(self.outside / "secret.md"), "~/x.md", "a\x00.md"):
            out = self.call("vault_read", path=p)
            self.assertTrue(out.startswith("refused"), (p, out))
            self.assertNotIn("outside secret", out)

    def test_hidden_and_denied_refused(self):
        self.assertIn("refused", self.call("vault_read", path=".obsidian/app.md"))
        self.assertIn("refused", self.call("vault_read", path=".git/config"))
        out = self.call("vault_read", path="private/creds/secrets.md")
        self.assertIn("off limits", out)
        self.assertIn("no matches", self.call("vault_search", query="SECRET"))
        listing = self.call("vault_list", recursive=True)
        self.assertNotIn("secrets.md", listing)
        self.assertNotIn(".obsidian", listing)

    def test_only_markdown(self):
        self.assertIn("refused", self.call("vault_read", path="image.png"))
        self.assertIn("refused", self.call("vault_create", path="x.txt",
                                           content="x"))

    def test_symlink_escape_refused(self):
        os.symlink(self.outside / "secret.md", self.root / "leak.md")
        os.symlink(self.outside, self.root / "out")
        self.assertIn("outside", self.call("vault_read", path="leak.md"))
        self.assertNotIn("outside secret", self.call("vault_read", path="leak.md"))
        self.assertNotIn("outside secret", self.call("vault_read",
                                                     path="out/secret.md"))
        self.assertNotIn("outside secret", self.call("vault_search",
                                                     query="outside"))
        for args in ({"path": "leak.md", "mode": "append", "content": "x"},):
            self.assertIn("refused", self.call("vault_edit", **args))
        self.assertIn("refused", self.call("vault_create",
                                           path="out/new.md", content="x"))
        self.assertEqual((self.outside / "secret.md").read_text(),
                         "outside secret\n")
        self.assertFalse((self.outside / "new.md").exists())

    def test_symlink_to_hidden_or_denied_refused(self):
        os.symlink(self.root / ".obsidian" / "app.md", self.root / "a.md")
        os.symlink(self.root / "private" / "creds" / "secrets.md",
                   self.root / "k.md")
        self.assertIn("refused", self.call("vault_read", path="a.md"))
        self.assertNotIn("SECRET", self.call("vault_read", path="k.md"))

    def test_internal_symlink_readable_not_writable(self):
        os.symlink("TODO.md", self.root / "AGENTS.md")
        out = self.call("vault_read", path="AGENTS.md")
        self.assertIn("buy milk", out)
        self.assertIn("real path TODO.md", out)
        self.assertIn("refused", self.call("vault_edit", path="AGENTS.md",
                                           mode="append", content="x"))
        self.assertTrue((self.root / "AGENTS.md").is_symlink())

    def test_dir_swapped_for_symlink_during_write(self):
        """A folder replaced by a symlink between checks is never followed."""
        real_walk = vault._walk_dirs

        def swap(root_fd, parts, create=False):
            if parts == ["projects"]:
                (self.root / "projects").rename(self.root / "projects.bak")
                os.symlink(self.outside, self.root / "projects")
            return real_walk(root_fd, parts, create)
        with mock.patch.object(vault, "_walk_dirs", swap):
            out = self.call("vault_create", path="projects/evil.md", content="x")
        self.assertIn("refused", out)
        self.assertFalse((self.outside / "evil.md").exists())

    def test_read_only_prefix(self):
        self.assertIn("frozen", self.call("vault_read", path="archive/old.md"))
        self.assertIn("read-only", self.call("vault_edit", path="archive/old.md",
                                             mode="append", content="x"))
        self.assertIn("read-only", self.call("vault_create",
                                             path="templates/t.md", content="x"))
        self.assertEqual((self.root / "archive" / "old.md").read_text(), "frozen\n")


class ReadSearchListTests(VaultTestCase):
    def test_read_has_sha_and_pages(self):
        (self.root / "long.md").write_text("".join(f"line {i}\n" for i in range(1, 101)))
        out = self.call("vault_read", path="long.md", limit=10)
        self.assertIn("sha256 ", out)
        self.assertIn("line 10", out)
        self.assertNotIn("line 11", out)
        self.assertIn("offset=11", out)
        out = self.call("vault_read", path="long.md", offset=95)
        self.assertIn("line 100", out)
        self.assertNotIn("line 94\n", out)

    def test_read_char_cap(self):
        self.v["max_read_chars"] = 50
        (self.root / "big.md").write_text("x" * 40 + "\n" + "y" * 40 + "\n")
        out = self.call("vault_read", path="big.md")
        self.assertIn("x" * 40, out)
        self.assertNotIn("y" * 40, out)
        self.assertIn("offset=2", out)

    def test_size_limit(self):
        self.v["max_note_bytes"] = 10
        self.assertIn("too large", self.call("vault_read", path="TODO.md"))

    def test_missing(self):
        self.assertIn("no such note", self.call("vault_read", path="nope.md"))
        self.assertIn("no such", self.call("vault_list", path="nope"))

    def test_search(self):
        out = self.call("vault_search", query="PORT")
        self.assertIn("projects/pi.md", out)
        self.assertIn("2: port 3401", out)
        self.assertIn("TODO.md (name match)", self.call("vault_search", query="todo"))
        self.assertIn("no matches", self.call("vault_search", query="zzzz"))
        self.assertIn("refused", self.call("vault_search", query="a"))
        out = self.call("vault_search", query="frozen", path="projects")
        self.assertIn("no matches", out)

    def test_search_and_list_caps(self):
        for i in range(10):
            (self.root / "projects" / f"n{i}.md").write_text("needle\n")
        self.v["max_results"] = 3
        out = self.call("vault_search", query="needle")
        self.assertEqual(out.count("needle"), 3)
        self.assertIn("stopped at 3", out)
        self.assertIn("stopped at 3", self.call("vault_list", path="projects"))

    def test_list(self):
        out = self.call("vault_list")
        self.assertIn("projects/", out)
        self.assertIn("TODO.md (", out)
        self.assertNotIn("image.png", out)
        self.assertIn("projects/pi.md", self.call("vault_list", recursive=True))


class WriteTests(VaultTestCase):
    def sha_of(self, rel):
        return vault.sha((self.root / rel).read_bytes())

    def test_create(self):
        out = self.call("vault_create", path="projects/new/idea.md",
                        content="# Idea\n")
        self.assertIn("created", out)
        self.assertEqual((self.root / "projects/new/idea.md").read_text(), "# Idea\n")
        self.assertIn("already exists", self.call(
            "vault_create", path="projects/new/idea.md", content="other"))
        self.assertEqual((self.root / "projects/new/idea.md").read_text(), "# Idea\n")
        self.assertTrue(any(a[1] == "vault_write" for a, _ in self.db.steps))

    def test_append_and_replace(self):
        self.call("vault_edit", path="TODO.md", mode="append",
                  content="- [ ] pay rent\n")
        self.call("vault_edit", path="TODO.md", mode="replace",
                  old_text="- [ ] buy milk", content="- [x] buy milk")
        self.assertEqual((self.root / "TODO.md").read_text(),
                         "- [x] buy milk\n- [ ] call Ana\n- [ ] pay rent\n")
        out = self.call("vault_edit", path="TODO.md", mode="replace",
                        old_text="- [ ]", content="x")
        self.assertIn("matches 2 times", out)
        self.assertIn("use vault_create", self.call(
            "vault_edit", path="nope.md", mode="append", content="x"))

    def test_append_adds_missing_newline(self):
        (self.root / "n.md").write_text("a")
        self.call("vault_edit", path="n.md", mode="append", content="b\n")
        self.assertEqual((self.root / "n.md").read_text(), "a\nb\n")

    def test_overwrite_needs_matching_sha(self):
        self.assertIn("needs base_sha256", self.call(
            "vault_edit", path="TODO.md", mode="overwrite", content="new"))
        base = self.sha_of("TODO.md")
        (self.root / "TODO.md").write_text("changed on the Mac\n")
        out = self.call("vault_edit", path="TODO.md", mode="overwrite",
                        content="new", base_sha256=base)
        self.assertIn("changed since it was read", out)
        self.assertEqual((self.root / "TODO.md").read_text(), "changed on the Mac\n")
        out = self.call("vault_edit", path="TODO.md", mode="overwrite",
                        content="new\n", base_sha256=self.sha_of("TODO.md"))
        self.assertIn("saved", out)
        self.assertEqual((self.root / "TODO.md").read_text(), "new\n")

    def test_change_during_edit_is_not_clobbered(self):
        """Someone else writes between our read and our rename."""
        real = vault._encode

        def racing(cfg_v, text):
            (self.root / "TODO.md").write_text("pulled from git\n")
            return real(cfg_v, text)
        with mock.patch.object(vault, "_encode", racing):
            out = self.call("vault_edit", path="TODO.md", mode="append",
                            content="x\n")
        self.assertIn("changed while editing", out)
        self.assertEqual((self.root / "TODO.md").read_text(), "pulled from git\n")
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.endswith(".tmp")], [])

    def test_write_limit_and_mode_preserved(self):
        os.chmod(self.root / "TODO.md", 0o600)
        self.v["max_write_bytes"] = 20
        self.assertIn("limit", self.call("vault_edit", path="TODO.md",
                                         mode="append", content="x" * 50))
        self.v["max_write_bytes"] = 1000
        self.call("vault_edit", path="TODO.md", mode="append", content="ok\n")
        self.assertEqual(os.stat(self.root / "TODO.md").st_mode & 0o777, 0o600)

    def test_concurrent_appends_all_land(self):
        def worker(i):
            self.call("vault_edit", path="TODO.md", mode="append",
                      content=f"- [ ] t{i}\n")
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        text = (self.root / "TODO.md").read_text()
        for i in range(8):
            self.assertIn(f"t{i}\n", text)


if __name__ == "__main__":
    unittest.main()
