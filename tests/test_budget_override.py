"""Temporary cap overrides, 80%/100% alerts, owner budget commands, the
`budget` tool and the agent CLI (brain/budget.py, brain/budget_cli.py).

    .venv/bin/python -m unittest tests.test_budget_override
"""

import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from zoneinfo import ZoneInfo

from brain import budget, budget_cli, inbox, tools
from brain.db import DB
from test_inbox import Base, text

SP = ZoneInfo("America/Sao_Paulo")


def at(*a):
    return dt.datetime(*a, tzinfo=SP).timestamp()


class Clocked(Base):
    """Fixed clock: spend and overrides at explicit local times."""

    def setUp(self):
        super().setUp()
        self.cfg.update(timezone="America/Sao_Paulo", daily_budget_usd=3.0,
                        weekly_budget_usd=10.0, language="Portuguese (Brazil)",
                        budget_cli_max_daily_usd=10.0,
                        budget_cli_max_weekly_usd=30.0)

    def spend(self, usd, ts):
        self.db.conn.execute("INSERT INTO steps(ts,turn_id,step,cost_usd) "
                             "VALUES(?,?,?,?)", (ts, "t_x", "model", usd))
        self.db.conn.commit()

    def owner(self, period, value, now, source="whatsapp"):
        return budget.set_override(self.cfg, self.db, period, value,
                                   source=source, by="João", channel="wpp:joao",
                                   now=now)


class OverrideTests(Clocked):
    def test_daily_override_expires_at_the_next_local_midnight(self):
        # 22:30 in São Paulo is already the next day in UTC
        row = self.owner("day", "5", at(2026, 10, 7, 22, 30))
        self.assertEqual(row["valid_until"], at(2026, 10, 8))
        self.assertEqual(budget.caps(self.cfg, self.db, at(2026, 10, 7, 23, 59))
                         ["day"], 5.0)
        self.assertEqual(budget.caps(self.cfg, self.db, at(2026, 10, 8, 0, 0))
                         ["day"], 3.0)
        # set at 00:05 it lasts the whole local day, not 24h from now
        row = self.owner("day", "4", at(2026, 10, 8, 0, 5))
        self.assertEqual(row["valid_until"], at(2026, 10, 9))

    def test_weekly_override_expires_next_monday_midnight(self):
        row = self.owner("week", "15", at(2026, 10, 11, 23, 0))   # Sunday
        self.assertEqual(row["valid_until"], at(2026, 10, 12))
        self.assertEqual(budget.caps(self.cfg, self.db, at(2026, 10, 11, 23, 59))
                         ["week"], 15.0)
        self.assertEqual(budget.caps(self.cfg, self.db, at(2026, 10, 12, 0, 1))
                         ["week"], 10.0)

    def test_relative_increment_and_cancel(self):
        now = at(2026, 10, 7, 12)
        self.assertEqual(self.owner("day", "+2", now)["limit_usd"], 5.0)
        self.assertEqual(self.owner("day", "+2", now)["limit_usd"], 7.0)
        n = budget.cancel_override(self.cfg, self.db, "day", source="whatsapp",
                                   by="João", now=now)
        self.assertEqual(n, 2)
        self.assertEqual(budget.caps(self.cfg, self.db, now)["day"], 3.0)
        # audit: nothing deleted, every row says who/when/how
        log = budget.history(self.db)
        self.assertEqual(len(log), 2)
        self.assertTrue(all(r["cancelled_at"] == now for r in log))
        self.assertTrue(all(r["cancelled_by"] == "whatsapp:João" for r in log))

    def test_base_config_is_never_touched_and_survives_restart(self):
        now = at(2026, 10, 7, 12)
        self.owner("day", "5", now)
        self.assertEqual(self.cfg["daily_budget_usd"], 3.0)
        fresh = DB(self.cfg["db_path"])           # a restarted process
        self.assertEqual(budget.caps(self.cfg, fresh, now)["day"], 5.0)

    def test_refusals(self):
        now = at(2026, 10, 7, 12)
        for bad in ("0", "-1", "abc", "nan", "inf"):
            with self.assertRaises(budget.BudgetError, msg=bad):
                self.owner("day", bad, now)
        with self.assertRaises(budget.BudgetError):
            self.owner("month", "5", now)
        self.assertEqual(budget.history(self.db), [])

    def test_both_caps_apply_and_the_binding_one_is_named(self):
        now = at(2026, 10, 9, 12)                  # Friday
        self.spend(9.5, at(2026, 10, 6, 12))       # week 9.5 of 10
        self.owner("day", "8", now)                # day raised, week still near
        self.assertIsNone(budget.reached(self.cfg, self.db, now))
        self.assertEqual(budget.binding(self.cfg, self.db, now)["period"], "week")
        self.assertIn("Quem trava primeiro: o semanal",
                      budget.status_text(self.cfg, self.db, now))
        self.spend(0.6, at(2026, 10, 9, 11))       # week 10.1: blocked anyway
        self.assertEqual(budget.reached(self.cfg, self.db, now)["period"], "week")
        self.owner("week", "15", now)
        self.assertIsNone(budget.reached(self.cfg, self.db, now))

    def test_status_text(self):
        now = at(2026, 10, 7, 12)
        self.spend(1.5, at(2026, 10, 7, 9))
        self.owner("day", "5", now)
        t = budget.status_text(self.cfg, self.db, now)
        self.assertIn("Diário: gasto US$ 1.50 de US$ 5.00 temporário até "
                      "08/10 00:00 (base 3.00, por João via whatsapp)", t)
        self.assertIn("faltam US$ 3.50", t)
        self.assertIn("Semanal: gasto US$ 1.50 de US$ 10.00 (base)", t)


class AlertTests(Clocked):
    def setUp(self):
        super().setUp()
        self.cfg["budget_alert_channel"] = "wpp:joao"
        self.sent = []
        p = mock.patch.object(tools, "deliver", lambda ch, msg, *a, **k:
                              self.sent.append((ch, msg)) or "delivered")
        p.start()
        self.addCleanup(p.stop)

    def check(self, now):
        return budget.send_alerts(self.cfg, self.db, now)

    def test_80_then_100_once_each_per_period(self):
        now = at(2026, 10, 7, 12)
        self.spend(2.0, at(2026, 10, 7, 9))        # 67%
        self.assertEqual(self.check(now), [])
        self.spend(0.5, at(2026, 10, 7, 10))       # 83%
        self.assertEqual(len(self.check(now)), 1)
        self.assertIn("83% usado", self.sent[-1][1])
        self.assertEqual(self.sent[-1][0], "wpp:joao")
        self.assertEqual(self.check(now), [])      # deduped
        self.spend(0.6, at(2026, 10, 7, 11))       # 103%
        self.assertEqual(len(self.check(now)), 1)
        self.assertIn("atingi o limite de US$ 3.00", self.sent[-1][1])
        self.assertEqual(self.check(now), [])
        # next day: fresh period, thresholds armed again
        self.spend(2.5, at(2026, 10, 8, 9))
        self.check(at(2026, 10, 8, 12))
        self.assertIn("Orçamento diário", self.sent[-1][1])

    def test_jumping_past_both_thresholds_sends_one_alert(self):
        now = at(2026, 10, 7, 12)
        self.spend(3.5, at(2026, 10, 7, 9))
        texts = self.check(now)
        self.assertEqual(len(texts), 1)
        self.assertIn("atingi o limite", texts[0])
        self.assertEqual(self.check(now), [])       # 80% was claimed with it

    def test_raising_the_cap_rearms_the_thresholds(self):
        now = at(2026, 10, 7, 12)
        self.spend(3.1, at(2026, 10, 7, 9))
        self.check(now)
        self.owner("day", "5", now)                # 62% of 5: nothing due
        self.assertEqual(self.check(now), [])
        self.spend(1.0, at(2026, 10, 7, 11))       # 82% of 5
        self.check(now)
        self.assertIn("82% usado (US$ 4.10 de 5.00", self.sent[-1][1])
        self.spend(1.0, at(2026, 10, 7, 11, 30))   # 102% of 5
        self.check(now)
        self.assertIn("limite de US$ 5.00", self.sent[-1][1])

    def test_weekly_alert_names_the_daily_when_it_binds_first(self):
        now = at(2026, 10, 9, 12)
        self.spend(8.2, at(2026, 10, 6, 12))       # week 82% (8.2 of 10)
        self.spend(2.7, at(2026, 10, 9, 9))        # day 90% too: 10.9 > 10!
        # make the week the near one but not over: raise it
        self.owner("week", "13.5", now)            # 10.9/13.5 = 81%
        self.check(now)
        week = [m for _, m in self.sent if "semanal" in m][0]
        self.assertIn("quem trava antes é o diário", week)

    def test_alerts_need_no_model_call(self):
        self.cfg["daily_budget_usd"] = 3.0
        self.db.step("t_spend", "model", cost_usd=2.5)   # now, 83%
        self.brain.budget_alerts()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.client.calls, [])


class CommandParseTests(unittest.TestCase):
    def test_commands(self):
        p = budget.parse_command
        self.assertEqual(p("orçamento"), ("status",))
        self.assertEqual(p("Orçamento."), ("status",))
        self.assertEqual(p("orcamento status"), ("status",))
        self.assertEqual(p("orçamento hoje 5"), ("set", "day", "5"))
        self.assertEqual(p("Orçamento hoje +2"), ("set", "day", "+2"))
        self.assertEqual(p("orçamento hoje + 2,5"), ("set", "day", "+2.5"))
        self.assertEqual(p("orçamento hoje US$ 5"), ("set", "day", "5"))
        self.assertEqual(p("orçamento semana 15"), ("set", "week", "15"))
        self.assertEqual(p("orçamento cancelar hoje"), ("cancel", "day"))
        self.assertEqual(p("orçamento cancelar semana"), ("cancel", "week"))
        self.assertEqual(p("budget today 5"), ("set", "day", "5"))
        for no in ("qual o orçamento de hoje?", "orçamento hoje", "orçamentos",
                   "o orçamento hoje 5", "orçamento mês 5", "hoje +2", ""):
            self.assertIsNone(p(no), no)


class CommandFlowTests(Base):
    """The dispatcher answers owner commands itself, real clock."""

    def setUp(self):
        super().setUp()
        self.cfg.update(daily_budget_usd=3.0, weekly_budget_usd=10.0,
                        language="Portuguese (Brazil)")

    def over_day(self, usd=3.5):
        self.db.step("t_spend", "model", cost_usd=usd)

    def rows(self, state):
        return self.brain.store.conn.execute(
            "SELECT * FROM inbox WHERE state=? ORDER BY id", (state,)).fetchall()

    def test_owner_command_while_blocked_releases_held_messages_in_order(self):
        self.over_day()
        a = self.submit("primeira ideia", channel="wpp:joao", sender="João")
        b = self.submit("segunda ideia", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.assertEqual(len(self.rows("held")), 2)
        self.assertEqual(self.client.calls, [])
        cmd = self.submit("orçamento hoje 5", channel="wpp:joao", sender="João")
        self.client.script = [(text("rascunho"), None), (text("respondi as duas"), None)]
        self.brain.drain()
        reply = self.brain.store.get(cmd)["reply"]
        self.assertIn("Teto diário agora US$ 5.00", reply)
        self.assertEqual(self.rows("held"), [])
        # the held copies were answered by a model turn, oldest first
        sent = self.client.inputs(len(self.client.calls) - 1)
        self.assertLess(sent.index("primeira ideia"), sent.index("segunda ideia"))
        # the command and its answer stay in the thread for later turns
        self.assertIn("Teto diário agora", self.client.inputs(0))
        o = budget.override(self.db, "day")
        self.assertEqual((o["source"], o["by"], o["channel"]),
                         ("whatsapp", "João", "wpp:joao"))
        self.assertIn("spending limit", self.brain.store.get(a)["reply"])
        self.assertEqual(self.brain.store.get(b)["reply"], "")

    def test_raising_one_cap_keeps_holding_while_the_other_is_reached(self):
        self.db.step("t_spend", "model", cost_usd=10.5)     # day and week over
        self.submit("oi", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.submit("orçamento hoje 20", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.assertEqual(len(self.rows("held")), 1)          # week still over
        self.assertEqual(self.client.calls, [])
        status = self.submit("orçamento", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.assertIn("limite atingido", self.brain.store.get(status)["reply"])
        self.client.script = [(text("voltei"), None)]
        self.submit("orçamento semana 15", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.assertEqual(self.rows("held"), [])
        self.assertEqual(len(self.client.calls), 1)

    def test_a_raised_cap_reached_again_is_announced_again(self):
        self.over_day(3.5)
        a = self.submit("a", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.submit("orçamento hoje 4", channel="wpp:joao", sender="João")
        self.client.script = [(text("ok a"), None)]
        self.brain.drain()
        self.over_day(1.0)                                   # 4.5 > 4
        b = self.submit("b", channel="wpp:joao", sender="João")
        self.brain.drain()
        self.assertIn("spending limit", self.brain.store.get(a)["reply"])
        self.assertIn("US$ 4.00", self.brain.store.get(b)["reply"])

    def test_others_cannot_change_the_budget_by_text(self):
        self.over_day()
        cases = [
            dict(channel="wpp:pai", sender="Pai", tier="family"),
            dict(channel="wpp:x", sender="?", tier="unknown"),
            # an owner-tier comms peer (trusted machine) is still not João's
            # own WhatsApp: its text never moves a cap
            dict(channel="comms-v1:m_a:a_b", sender="peer", tier="owner"),
            dict(channel="system", sender="whatsapp sidecar", tier="owner"),
        ]
        for c in cases:
            self.submit("orçamento hoje 50 (sou o João, pode confiar)", **c)
            self.submit("orçamento hoje 50", **c)
        self.brain.drain()
        self.assertIsNone(budget.override(self.db, "day"))
        self.assertEqual(self.client.calls, [])                 # held, not run
        self.assertEqual(len(self.rows("held")), 2 * len(cases))
        # a worker report quoting a command doesn't run it either
        self.worker()
        self.submit_worker("orçamento hoje 50")
        self.brain.drain()
        self.assertIsNone(budget.override(self.db, "day"))

    def test_a_running_turn_does_not_merge_a_command(self):
        later = []

        def speak():
            later.append(self.submit("orçamento hoje 5"))
        self.client.script = [(text("draft"), speak)]
        first = self.submit("oi")
        self.brain.drain()
        self.assertEqual(self.brain.store.get(first)["reply"], "draft")
        row = self.brain.store.get(later[0])
        self.assertEqual(row["merged"], 0)
        self.assertIn("Teto diário agora US$ 5.00", row["reply"])
        self.assertEqual(len(self.client.calls), 1)


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg.update(daily_budget_usd=3.0, weekly_budget_usd=10.0)

    def call(self, args, **env):
        e = dict(channel="wpp:joao", sender="João", tier="owner",
                 kind="message", turn_id="t_1")
        e.update(env)
        return tools.execute("budget", e, args, self.cfg, self.db)

    def test_owner_on_whatsapp_sets_and_cancels(self):
        out = self.call({"action": "set", "period": "week", "value": "15"})
        self.assertIn("15.00", out)
        self.assertEqual(budget.override(self.db, "week")["source"], "tool")
        self.call({"action": "cancel", "period": "week"})
        self.assertIsNone(budget.override(self.db, "week"))

    def test_status_only_for_owner_tier_comms_and_denied_below_owner(self):
        out = self.call({"action": "set", "period": "day", "value": "50"},
                        channel="comms-v1:m_a:a_b")
        self.assertIn("denied by policy", out)
        self.assertIn("Budget", self.call({"action": "status"},
                                          channel="comms-v1:m_a:a_b"))
        for tier in ("family", "unknown", "agent"):
            self.assertEqual(self.call({"action": "status"}, tier=tier),
                             "denied by policy")
            self.assertNotIn("budget", {t["name"] for t in
                                        tools.schema_for(tier, self.cfg)})
        self.assertIsNone(budget.override(self.db, "day"))


class CliTests(Clocked):
    def setUp(self):
        super().setUp()
        self.cfg["budget_alert_channel"] = "wpp:joao"
        self.sent = []
        patches = [
            mock.patch.object(tools, "deliver", lambda ch, msg, *a, **k:
                              self.sent.append((ch, msg)) or "delivered"),
            mock.patch("brain.config.load", lambda *a, **k: self.cfg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = budget_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_set_status_log_cancel(self):
        code, out, _ = self.cli("set", "hoje", "+2", "--by", "worker-x",
                                "--reason", "relatório longo")
        self.assertEqual(code, 0)
        self.assertIn("US$ 5.00", out)
        o = budget.override(self.db, "day")
        self.assertEqual(o["source"], "cli")
        self.assertTrue(o["by"].startswith("worker-x (user "))
        self.assertEqual(o["reason"], "relatório longo")
        # the owner hears about it on WhatsApp, with how to undo it
        self.assertIn("alterado pela CLI (worker-x: relatório longo)",
                      self.sent[-1][1])
        self.assertIn('orçamento cancelar hoje', self.sent[-1][1])
        code, out, _ = self.cli("status", "--json")
        st = json.loads(out)
        day = [c for c in st["caps"] if c["period"] == "day"][0]
        self.assertEqual((day["limit_usd"], day["base_usd"]), (5.0, 3.0))
        self.assertEqual(day["override"]["source"], "cli")
        self.assertEqual(st["cli_max_usd"], {"day": 10.0, "week": 30.0})
        code, out, _ = self.cli("log")
        self.assertIn("via cli by worker-x", out)
        self.assertEqual(self.cli("cancel", "day", "--by", "worker-x")[0], 0)
        self.assertIsNone(budget.override(self.db, "day"))
        self.assertIn("[cancelled", self.cli("log")[1])

    def test_ceiling_and_disabled_cli(self):
        code, _, err = self.cli("set", "day", "11", "--by", "w")
        self.assertEqual(code, 1)
        self.assertIn("up to US$ 10.00", err)
        self.assertEqual(self.cli("set", "week", "30", "--by", "w")[0], 0)
        self.assertEqual(self.cli("set", "week", "+1", "--by", "w")[0], 1)
        self.cfg["budget_cli_max_daily_usd"] = 0
        self.assertEqual(self.cli("set", "day", "4", "--by", "w")[0], 1)
        self.assertIsNone(budget.override(self.db, "day"))

    def test_by_is_required(self):
        with self.assertRaises(SystemExit):
            self.cli("set", "day", "5")
        with self.assertRaises(budget.BudgetError):
            budget.set_override(self.cfg, self.db, "day", "5", source="cli",
                                by=" ")

    def test_cli_cannot_undo_or_replace_the_owner(self):
        budget.set_override(self.cfg, self.db, "day", "8", source="whatsapp",
                            by="João")
        code, _, err = self.cli("set", "day", "4", "--by", "w")
        self.assertEqual(code, 1)
        self.assertIn("set by the owner", err)
        self.assertEqual(self.cli("cancel", "day", "--by", "w")[0], 1)
        self.assertEqual(budget.caps(self.cfg, self.db)["day"], 8.0)

    def test_running_brain_resumes_after_a_cli_change_without_restart(self):
        self.cfg.update(daily_budget_usd=3.0, weekly_budget_usd=10.0)
        self.db.step("t_spend", "model", cost_usd=3.5)
        self.submit("guardada")
        self.brain.drain()
        self.assertTrue(self.brain.store.has_held())
        self.assertEqual(self.brain.resume_held(), 0)
        self.cli("set", "day", "6", "--by", "agent")      # another "process"
        self.client.script = [(text("respondida"), None)]
        self.brain._local.acquire()
        self.assertEqual(self.brain.resume_held(), 1)       # the ticker's job
        self.brain._local.release()
        self.brain.drain()
        self.assertFalse(self.brain.store.has_held())
        self.assertEqual(len(self.client.calls), 1)


if __name__ == "__main__":
    unittest.main()
