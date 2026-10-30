"""The Activity Monitor without its window: qbd's state folders read as they change, the views its pages show, and
what a person's answers do. The window itself is checked in test_window.py, where wxPython is installed."""
import contextlib
import dataclasses
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from qbd_agent_kit import cli
from qbd_agent_kit.api import COMMANDS, Drafts, file_digest, fingerprint
from qbd_activity_monitor import actions
from qbd_activity_monitor import words as w
from qbd_activity_monitor.feed import Feed, Log, Rec
from qbd_activity_monitor.model import ALL, Model, State
from qbd_activity_monitor.sample import ENV, design_now, design_seen, write_sample


def line(t: float, **e: object) -> str:
    return json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t)), **e}) + "\n"


class Logs(unittest.TestCase):
    """A call log or write log read as qbd adds to it, and noticed when something else shortens or replaces it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "calls.jsonl")
        self.t = time.time() - 60

    def add(self, text: str, path: str = "") -> None:
        with open(path or self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def log(self, mark: list | None = None) -> Log:
        return Log(self.path, self.dir.name, False, 0, mark)

    def test_a_log_is_read_as_it_grows_and_half_a_line_waits_for_its_end(self):
        self.add(line(self.t, command="status") + line(self.t, command="count"))
        log = self.log()
        self.assertEqual([r.e["command"] for r in log.poll()], ["status", "count"])
        whole = line(self.t, command="search")
        self.add(whole[:20])
        self.assertEqual(log.poll(), [])
        self.add(whole[20:])
        self.assertEqual([(r.e["command"], r.line) for r in log.poll()], [("search", 3)])
        self.assertEqual(log.cuts, [])

    def test_after_a_crash_the_entry_appended_to_half_a_line_is_still_read(self):
        self.add('{"time": "2026-10-21T08:00:00.000Z", "comm' + line(self.t, command="status") + "not json\n")
        log = self.log()
        self.assertEqual([r.e["command"] for r in log.poll()], ["status"])
        self.assertEqual([s.line for s in log.skipped], [2])

    def test_a_log_qbd_moved_is_read_to_its_end_then_the_new_one(self):
        self.add(line(self.t, command="status"))
        log = self.log()
        log.poll()
        self.add(line(self.t, command="count"))  # written just before the move
        os.replace(self.path, self.path + ".20261021-100000")
        self.add(line(self.t, command="search"))
        self.assertEqual([r.e["command"] for r in log.poll()], ["count", "search"])
        self.assertEqual(log.cuts, [], "qbd moving its log whole is no cut")

    def test_a_log_made_shorter_or_replaced_is_reported(self):
        self.add(line(self.t, command="status") + line(self.t, command="count"))
        log = self.log()
        log.poll()
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(line(self.t, command="status"))
        log.poll()
        self.assertEqual([name for _, name in log.cuts], ["calls.jsonl"])
        other = os.path.join(self.dir.name, "other.jsonl")
        self.add(line(self.t, command="search") * 3, other)
        os.replace(other, self.path)
        log.poll()
        self.assertEqual(len(log.cuts), 2, "replaced by another file")

    def test_a_cut_while_the_monitor_was_closed_is_noticed_when_it_starts(self):
        self.add(line(self.t, command="status") + line(self.t, command="count"))
        log = self.log()
        log.poll()
        mark = log.marked()
        self.add(line(self.t, command="search"))
        grown = self.log(mark)
        grown.poll()
        self.assertEqual(grown.cuts, [], "a log that only grew")
        os.replace(self.path, self.path + ".20261021-100000")
        self.add(line(self.t, command="status"))
        moved = self.log(mark)
        moved.poll()
        self.assertEqual(moved.cuts, [], "moved whole by qbd: the archive has it")
        os.remove(self.path + ".20261021-100000")
        gone = self.log(mark)
        gone.poll()
        self.assertEqual(len(gone.cuts), 1, "gone with no archive")


class Sample(unittest.TestCase):
    """The design's morning at Bayside Hardware & Supply, as the pages show it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = design_now()
        write_sample(self.dir.name, self.now)
        state = State(None)
        state.data["problems_seen"] = design_seen(self.now)
        feed = Feed(ENV, self.now - 90 * 86400, only=[self.dir.name], sample=True)
        self.m = Model(feed, state, settings=lambda: dict(ENV), clock=lambda: self.now, tools=COMMANDS)
        self.m.poll()
        self.bayside = self.m.default_file()

    def test_two_files_of_one_company_go_by_what_sets_them_apart(self):
        self.assertEqual([f.name for f in self.m.files()], ["Bayside Hardware & Supply", "Bayside Hardware & Supply – 2025 archive"])

    def test_the_overview_counts_today_and_shows_what_runs_and_waits(self):
        o = self.m.overview(self.bayside)
        tiles = {name: (value, note) for name, value, note, _ in o.tiles}
        self.assertEqual(tiles["Records read"][0], "562")
        self.assertEqual(tiles["Changes saved"], ("1", "2 more waiting for you"))
        self.assertEqual((o.running.title, o.running.page, o.running.percent), ("Find duplicate entries", 6, 43))
        self.assertEqual([c.who.name for c in o.waiting], ["n8n nightly sync", "Gemini (agy)"])
        self.assertEqual([n["kind"] for n in o.needs], ["draft", "draft"], "the blocked request worked later")
        self.assertEqual(o.sessions_today, 7)

    def test_the_monitors_own_lines_are_not_requests(self):
        today = len(self.m.calls(ALL, self.m.midnight()))
        self.add_call(agent="Activity Monitor", command="resume", outcome="ok", args={})
        self.assertEqual(len(self.m.calls(ALL, self.m.midnight())), today)
        self.add_call(agent="Claude (Claude Code)", command="status", outcome="ok", args={})
        self.assertEqual(len(self.m.calls(ALL, self.m.midnight())), today + 1)

    def add_call(self, **e: object) -> None:
        with open(os.path.join(self.dir.name, "calls.jsonl"), "a", encoding="utf-8", newline="\n") as f:
            f.write(line(self.now - 5, **e))
        self.m.poll()

    def test_problems_fold_repeats_and_say_what_worked_later(self):
        rows = self.m.problems(ALL, self.now - 7 * 86400)
        self.assertEqual(len(rows), 7)
        blocked = rows[0]
        self.assertEqual((blocked.title, blocked.badge[0], blocked.status[0], blocked.new),
                         ("Company file profile", "Blocked by a window", "Worked at 09:51", True))
        timeouts = next(p for p in rows if p.badge[0] == "Timed out")
        self.assertEqual(timeouts.count, 2)
        self.assertIn("2 times since 10:15", timeouts.args)
        refused = next(p for p in rows if p.title == "Save an approved draft to QuickBooks" and p.badge[0] != "Not confirmed")
        self.assertEqual((refused.badge[0], refused.status[0]), ("Not saved", "see Changes"), "the same word as its change")
        self.assertEqual([p.title for p in self.m.new_problems()], ["Company file profile"])

    def test_each_draft_waits_once_with_the_fingerprint_of_what_is_shown(self):
        waiting, expired, answered = self.m.approvals(self.bayside)
        self.assertEqual([a.token for a in waiting], ["a1b2c3d4", "e5f6a7b8"])
        for a in waiting:
            with open(os.path.join(self.dir.name, "drafts", f"{a.token}.json"), "rb") as f:
                self.assertEqual(a.seen, fingerprint([file_digest(f.read())]))
            self.assertFalse(a.can_approve, "nothing is saved from the monitor's sample")
        deposit, bill = waiting
        self.assertEqual((deposit.title, deposit.total, deposit.currency, deposit.columns),
                         ("Add deposit to Chequing", "3,231.95", "CAD", ("Payment from", "Amount")))
        self.assertEqual(deposit.lines[0], ("Foxhall Landscaping", "1,200.00"))
        self.assertEqual((bill.flag, bill.columns), ("Possible duplicate", ("Account", "Description", "Amount")))
        self.assertEqual(expired, [])
        self.assertIn(("Add invoice for Ironwood Fitness", "Expired, not saved"), [(x.title, x.badge[0]) for x in answered])

    def test_changes_say_what_to_do_when_not_saved_or_not_confirmed(self):
        rows = self.m.changes(self.bayside, self.now - 7 * 86400)
        self.assertEqual([c.badge[0] for c in rows], ["Saved", "Saved", "Saved", "Not saved", "Check it"])
        contact, _, customer, failed, unknown = rows
        self.assertEqual([d[0] for d in contact.diff], ["Contact"])
        self.assertTrue(contact.undo)
        self.assertFalse(customer.undo)
        self.assertIn("deletes are not allowed on this PC", customer.undo_text)
        self.assertIn("Nothing was saved", failed.explain)
        self.assertIn("QuickBooks status 3140", failed.todo)
        self.assertIn("could save it twice", unknown.todo)

    def test_a_change_counts_its_lines_with_amounts_not_its_fields(self):
        e = {"action": "add", "entity": "bill", "outcome": "done", "total": 214.6, "agent": "Claude (Claude Code)",
             "summary": "Add bill for Brightway Office Products dated 2026-10-21, number BW-5602, total 214.60\n"
                        "- Office Supplies: 214.60\n- DueDate: 2026-11-20"}
        self.assertEqual(self.m.change_row(Rec(self.now, e, self.dir.name, True, "audit.jsonl", 1)).about, "1 line")

    def test_the_tray_says_what_waits_without_amounts(self):
        t = self.m.tray()
        self.assertEqual((t.state, t.badge[0]), ("waiting", "Waiting for you"))
        self.assertEqual([x[2] for x in t.lines], ["2 waiting in line", "2 changes wait for your answer", "1 new problem"])
        self.assertNotRegex(t.tooltip, r"\d,\d\d\d\.\d\d")

    def test_the_portal_shows_what_runs_then_what_waits_for_someone(self):
        p = self.m.portal()
        self.assertEqual((p.state, p.title, p.detail, p.line, p.need),
                         ("running", "Find duplicate entries", "Claude (Claude Code) · Transactions, page 6 of 14", 2, 2))
        actions.stop(p.cmd)
        self.m.poll()
        self.assertEqual(self.m.portal().state, "stopping")
        os.remove(os.path.join(self.dir.name, "live", "4412.json"))
        self.m.poll()
        p = self.m.portal()
        self.assertEqual((p.state, p.title, p.detail), ("stopped", "Find duplicate entries", "Claude (Claude Code) · after step 2"))
        self.assertEqual(p.text, "Stopped by you. Nothing was saved. Claude's next command waits until you resume.")
        actions.resume(*p.resume)
        self.m.poll()
        p = self.m.portal()
        self.assertEqual((p.state, p.title, p.page), ("waiting", "2 changes wait for your answer", "approvals"))
        for token in ("a1b2c3d4", "e5f6a7b8"):
            os.remove(os.path.join(self.dir.name, "drafts", f"{token}.json"))
        self.m.poll()
        self.assertEqual(self.m.portal().state, "line")
        for pid in (5120, 5133):
            os.remove(os.path.join(self.dir.name, "live", f"{pid}.json"))
        self.m.poll()
        p = self.m.portal()
        self.assertEqual((p.state, p.badge), ("done", ("Done", "ok", "check_circle")))
        self.assertNotRegex(p.title + p.detail + p.text, r"\d,\d\d\d\.\d\d", "never an amount")

    def test_a_log_cut_shows_for_a_week(self):
        self.m.state.note_cuts([(self.now - 3600, "audit.jsonl", self.dir.name)], self.now)
        self.assertIn("log_cut", [b.kind for b in self.m.banners(self.bayside)])
        self.assertFalse(self.m.state.note_cuts([(self.now, "audit.jsonl", self.dir.name)], self.now), "known already")
        self.m.state.data["cuts"][0]["t"] = self.now - 8 * 86400
        self.assertNotIn("log_cut", [b.kind for b in self.m.banners(self.bayside)])


class Answers(unittest.TestCase):
    """Approve, reject and dismiss, on drafts qbd made with its sample company (--demo)."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("QBD_")}
        patch = mock.patch.dict(os.environ, {**env, "LOCALAPPDATA": self.home.name}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.folder = os.path.join(self.home.name, "qbd-agent-kit")
        self.drafts = os.path.join(self.folder, "drafts")

    def qbd(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def draft(self) -> str:
        with mock.patch.dict(os.environ, {"QBD_AGENT": "Claude (Claude Code)", "QBD_SESSION": "cc-7f3c"}):
            code, out = self.qbd("--demo", "draft", "add", "customer", "--fields", '{"Name": "ZZ-TEST Monitor"}', "--reason", "test")
        self.assertEqual(code, 0, out)
        return out["token"]

    def approvals(self) -> tuple[list, list, list]:
        m = Model(Feed(os.environ, 0), State(None))
        m.poll()
        return m.approvals(ALL)

    def edit(self, token: str, **changes: object) -> None:
        path = os.path.join(self.drafts, f"{token}.json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**d, **changes}, f)

    def saved(self) -> list[dict]:
        try:
            with open(os.path.join(self.folder, "audit-demo.jsonl"), encoding="utf-8") as f:
                return [json.loads(x) for x in f]
        except FileNotFoundError:
            return []

    def test_approving_saves_the_draft_as_it_was_shown_once(self):
        token = self.draft()
        a = self.approvals()[0][0]
        self.assertEqual((a.token, a.can_approve), (token, True))
        self.assertEqual(a.seen, Drafts(self.drafts).fingerprint(token))
        out = actions.approve(a)
        self.assertNotIn("error", out)
        self.assertEqual([(e["approved_in"], e["outcome"], e["agent"]) for e in self.saved()], [("monitor", "done", "Claude (Claude Code)")])
        waiting, _, answered = self.approvals()
        self.assertEqual(waiting, [])
        self.assertTrue(answered[0].how.endswith("in the monitor"), answered[0].how)

    def test_a_draft_changed_after_it_was_shown_is_not_saved(self):
        token = self.draft()
        a = self.approvals()[0][0]
        self.edit(token, request=a.d["request"].replace("ZZ-TEST Monitor", "ZZ-TEST Changed"))
        out = actions.approve(a)
        self.assertIn("changed after it was shown", out["error"]["message"])
        self.assertEqual(self.saved(), [])
        again = self.approvals()[0][0]
        self.assertNotEqual(again.seen, a.seen)
        self.assertNotIn("error", actions.approve(again), "looked at again, it can be approved")

    def test_rejecting_discards_the_draft_once(self):
        self.draft()
        a = self.approvals()[0][0]
        self.assertEqual(actions.reject(a), {"rejected": True})
        self.assertIn("error", actions.reject(a))
        waiting, _, answered = self.approvals()
        self.assertEqual((waiting, answered[0].badge[0]), ([], "Rejected"))

    def test_only_an_expired_draft_is_dismissed(self):
        token = self.draft()
        actions.dismiss(self.approvals()[0][0])
        self.assertEqual([a.token for a in self.approvals()[0]], [token], "a draft still waiting stays")
        self.edit(token, expires=time.time() - 1)
        waiting, expired, _ = self.approvals()
        self.assertEqual((waiting, [a.token for a in expired]), ([], [token]))
        actions.dismiss(expired[0])
        _, expired, answered = self.approvals()
        self.assertEqual((expired, answered[0].badge[0]), ([], "Expired, not saved"))

    def test_a_file_not_filed_under_its_own_token_is_no_draft(self):
        self.draft()
        kept = os.path.join(self.folder, "keep.json")
        for name, d in ((kept, {}), (os.path.join(self.drafts, "0badc0de.json"), {"token": "../keep", "expires": time.time() + 600}),
                        (os.path.join(self.drafts, "notes.json"), {"token": "notes", "expires": time.time() + 600})):
            with open(name, "w", encoding="utf-8") as f:
                json.dump({"action": "add", "entity": "customer", "summary": "Add customer X", **d}, f)
        waiting = self.approvals()[0]
        self.assertEqual(len(waiting), 1)
        actions.dismiss(dataclasses.replace(waiting[0], token="../keep"))
        self.assertTrue(os.path.exists(kept), "nothing outside the drafts folder is touched")

    def test_an_import_waits_as_one_approval_for_all_its_records(self):
        path = os.path.join(self.home.name, "customers.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Name\nZZ-TEST One\nZZ-TEST Two\n")
        code, out = self.qbd("--demo", "import", "customer", "--csv", path)
        self.assertEqual(code, 0, out)
        waiting = self.approvals()[0]
        self.assertEqual([a.token for a in waiting], [out["token"]])
        self.assertEqual(waiting[0].seen, Drafts(self.drafts).fingerprint(out["token"]), "its records are part of what is shown")
        self.assertEqual(waiting[0].columns, ("Record", ""))


class Words(unittest.TestCase):
    def test_long_numbers_are_masked_and_dates_are_not(self):
        self.assertEqual(w.mask("card 4111 1111 1111 1111 from 2026-07-23"), "card ***1111 from 2026-07-23")

    def test_days_and_months_are_english_whatever_the_language(self):
        now = design_now()
        self.assertEqual(w.day(now - 2 * 86400, now), "Monday 19 October")
        self.assertEqual(w.when(now - 86400, now), f"Yesterday {w.clock(now - 86400)}")
        self.assertTrue(w.exact(now).startswith("Wednesday 21 October 2026, "))


if __name__ == "__main__":
    unittest.main()
