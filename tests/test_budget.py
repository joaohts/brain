"""Spending caps: daily + weekly periods in the configured timezone, and
messages that arrive before, at and after a cap is reached are held — never
consumed unanswered — then answered once spend is back under the caps.

    .venv/bin/python -m unittest tests.test_budget
"""

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from brain import budget, inbox
from brain.db import DB
from test_inbox import Base, text

SP = ZoneInfo("America/Sao_Paulo")


def at(*a):
    return dt.datetime(*a, tzinfo=SP).timestamp()


class PeriodTests(unittest.TestCase):
    cfg = {"timezone": "America/Sao_Paulo", "daily_budget_usd": 3.0,
           "weekly_budget_usd": 10.0}

    def test_day_and_week_start_at_local_midnight_and_monday(self):
        p = budget.periods(self.cfg, at(2026, 10, 7, 23, 30))   # a Wednesday
        self.assertEqual(p["day"], (at(2026, 10, 7), at(2026, 10, 8)))
        self.assertEqual(p["week"], (at(2026, 10, 5), at(2026, 10, 12)))
        # 22:00 local is already the next UTC day: still the same local day
        self.assertEqual(budget.periods(self.cfg, at(2026, 10, 7, 22))["day"][0],
                         at(2026, 10, 7))
        sunday = budget.periods(self.cfg, at(2026, 10, 11, 23, 59))
        self.assertEqual(sunday["week"][0], at(2026, 10, 5))


class CapTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg.update(timezone="America/Sao_Paulo", daily_budget_usd=3.0,
                        weekly_budget_usd=10.0)

    def spend(self, usd, ts):
        self.db.conn.execute("INSERT INTO steps(ts,turn_id,step,cost_usd) "
                             "VALUES(?,?,?,?)", (ts, "t_x", "model", usd))
        self.db.conn.commit()

    def test_spending_can_be_concentrated_until_the_weekly_cap(self):
        now = at(2026, 10, 9, 12)                    # Friday
        for day in (5, 6, 7):                        # Mon-Wed: 2.9 each
            self.spend(2.9, at(2026, 10, day, 12))
        self.assertIsNone(budget.reached(self.cfg, self.db, now))   # 8.7 < 10
        self.spend(1.4, at(2026, 10, 9, 9))          # 10.1 > 10, day 1.4 < 3
        cap = budget.reached(self.cfg, self.db, now)
        self.assertEqual((cap["period"], cap["resets_at"]),
                         ("week", at(2026, 10, 12)))
        # the next Monday the week starts over
        self.assertIsNone(budget.reached(self.cfg, self.db, at(2026, 10, 12, 0, 1)))

    def test_daily_cap_still_applies_and_zero_disables(self):
        now = at(2026, 10, 9, 12)
        self.spend(3.2, at(2026, 10, 9, 8))
        self.assertEqual(budget.reached(self.cfg, self.db, now)["period"], "day")
        self.cfg["daily_budget_usd"] = 0
        self.assertIsNone(budget.reached(self.cfg, self.db, now))
        self.assertIsNone(budget.reached(self.cfg, self.db, at(2026, 10, 10, 0, 1)))

    def test_notice_names_the_cap_and_when_it_resets(self):
        self.cfg["language"] = "Portuguese (Brazil)"
        self.cfg["messages"]["budget_reached"] = (
            "Limite {period} (US$ {limit}) até {resets}.")
        self.spend(10.5, at(2026, 10, 6, 9))
        from brain.config import message
        cap = budget.reached(self.cfg, self.db, at(2026, 10, 6, 12))
        self.assertEqual(message(self.cfg, "budget_reached",
                                 **budget.describe(self.cfg, cap)),
                         "Limite semanal (US$ 10.00) até 12/10 00:00.")


class HoldTests(Base):
    """Real clock: spend is injected as traced steps 'now'."""

    def setUp(self):
        super().setUp()
        self.cfg.update(daily_budget_usd=3.0, weekly_budget_usd=10.0)

    def over(self, usd=5.0):
        self.db.step("t_spend", "model", cost_usd=usd)

    def under(self):
        self.db.conn.execute("DELETE FROM steps WHERE turn_id='t_spend'")
        self.db.conn.commit()

    def rows(self, state):
        return self.brain.store.conn.execute(
            "SELECT * FROM inbox WHERE state=? ORDER BY id", (state,)).fetchall()

    def test_before_at_and_after_the_cap_nothing_is_lost(self):
        before = self.submit("first, under the cap")
        merged = []

        def cross():   # the turn pushes spend over the cap; the speaker adds a line
            self.over()
            merged.append(self.submit("one more thing"))
        self.client.script = [(text("answer 1"), cross), (text("answer 1+"), None)]
        self.brain.drain()
        # a running turn is never cut short: both are answered by it
        self.assertEqual(self.brain.store.get(before)["reply"], "answer 1+")
        self.assertEqual(self.brain.store.get(merged[0])["merged"], 1)

        after1 = self.submit("sent after the cap")
        after2 = self.submit("and another")
        self.brain.drain()
        self.assertEqual(len(self.client.calls), 2)          # no model call
        r1, r2 = self.brain.store.get(after1), self.brain.store.get(after2)
        self.assertIn("spending limit", r1["reply"])          # told once
        self.assertEqual(r2["reply"], "")                     # not again
        self.assertEqual(r1["state"], "done")
        held = self.rows("held")
        self.assertEqual([h["text"] for h in held],
                         ["sent after the cap", "and another"])
        self.assertEqual(len(self.steps("budget_held")), 2)

        # still over: the ticker leaves them alone, no new notice
        self.assertEqual(self.brain.resume_held(), 0)
        self.submit("third")
        self.brain.drain()
        self.assertEqual(len(self.rows("held")), 3)
        self.assertEqual(self.rows("done")[-1]["reply"], "")

        # the cap resets: all three answered in one turn, in order, and the
        # thread keeps them
        self.under()
        self.client.script = [(text("draft"), None), (text("caught up"), None)]
        self.brain._local.acquire()          # resume without a background kick
        self.assertEqual(self.brain.store.release_held(), 3)
        self.brain._local.release()
        self.brain.drain()
        # the head turn merges the other two (one draft discarded on the way)
        sent = self.client.inputs(len(self.client.calls) - 1)
        self.assertLess(sent.index("sent after the cap"), sent.index("and another"))
        self.assertLess(sent.index("and another"), sent.index("third"))
        self.assertIn("held by the spending limit", sent)
        self.assertEqual(self.rows("held"), [])
        self.assertEqual(self.rows("unread"), [])
        thread = [m["text"] for m in self.db.window("cli", 50)]
        self.assertTrue(any("sent after the cap" in t for t in thread))
        self.assertIn("caught up", thread)
        self.assertEqual(len(self.steps("origin_delivery")), 1)   # reply pushed

    def test_held_rows_survive_a_restart_and_resume_on_start(self):
        self.over()
        row = self.submit("keep me")
        self.brain.drain()
        self.assertEqual(len(self.rows("held")), 1)
        self.under()
        again = inbox.Brain(self.cfg, DB(self.cfg["db_path"]),
                            client_factory=lambda: self.client, lease_seconds=60)
        self.client.script = [(text("answered after restart"), None)]
        again._local.acquire()
        self.assertEqual(again.resume_held(), 1)   # kick busy: rows just unread
        again._local.release()
        again.drain()
        copy = self.rows("done")[-1]
        self.assertEqual((copy["text"], copy["reply"]),
                         ("keep me", "answered after restart"))
        self.assertIn("spending limit", self.brain.store.get(row)["reply"])

    def test_a_new_period_gets_a_new_notice(self):
        self.over()
        a = self.submit("a")
        self.brain.drain()
        self.brain.store.conn.execute("UPDATE budget_notices SET period='day:0'")
        b = self.submit("b")
        self.brain.drain()
        self.assertIn("spending limit", self.brain.store.get(a)["reply"])
        self.assertIn("spending limit", self.brain.store.get(b)["reply"])

    def test_waiting_callers_get_the_notice_instead_of_blocking(self):
        self.over()
        self.brain.kick()
        self.assertIn("spending limit", self.brain.run_sync(self.env("hi"), 5))

    def test_worker_result_is_held_and_its_notice_goes_to_the_origin(self):
        self.worker()
        self.over()
        row = self.submit_worker("[FINAL] done")
        self.brain.drain()
        self.assertEqual(self.brain.store.get(row)["state"], "done")
        self.assertEqual(len(self.steps("origin_delivery")), 1)   # the notice
        held = self.rows("held")[0]
        self.assertEqual(held["kind"], "worker_result")
        self.assertEqual(self.brain.store.worker("w1")["state"], "running")
        self.under()
        self.client.script = [(text("the worker finished"), None)]
        self.brain._local.acquire()
        self.brain.store.release_held()
        self.brain._local.release()
        self.brain.drain()
        self.assertEqual(self.brain.store.worker("w1")["state"], "idle")

    def test_holding_a_row_that_is_no_longer_unread_is_a_no_op(self):
        row = self.brain.store.get(self.submit("x"))
        self.brain.store.mark_read([row["id"]], "t_1", "h")
        self.assertIsNone(self.brain.store.hold(row, "day:1", "n"))
        self.assertEqual(self.rows("held"), [])


if __name__ == "__main__":
    unittest.main()
