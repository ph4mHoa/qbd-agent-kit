"""What qbd writes for the Activity Monitor: who ran each command and where, the live file while it runs, the line for
QuickBooks, stop and pause, and drafts answered in the monitor."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from qbd_agent_kit import cli
from qbd_agent_kit.config import load_config
from qbd_agent_kit.demo.transport import DemoTransport
from qbd_agent_kit.errors import QbConnectionError, Stopped, ToolError
from qbd_agent_kit.qb.client import QbClient
from qbd_agent_kit.safety import live
from qbd_agent_kit.tools.common import Drafts, batch_tokens
from qbd_agent_kit.tools.write import reject


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("QBD_")}
        patch = mock.patch.dict(os.environ, {**env, "LOCALAPPDATA": self.home.name}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.folder = os.path.join(self.home.name, "qbd-agent-kit")

    def run_qbd(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def calls(self, name: str = "calls-demo.jsonl") -> list[dict]:
        with open(os.path.join(self.folder, name), encoding="utf-8") as f:
            return [json.loads(x) for x in f]


class WhoAndWhere(Base):
    def test_the_call_log_names_the_assistant_session_company_and_settings(self):
        env = {"QBD_AGENT": "Gemini (agy)", "QBD_SESSION": "agy-0831", "QBD_SESSION_TITLE": "Morning   check: who owes us money"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_qbd("--demo", "daily-brief")[0], 0)
            self.assertEqual(self.run_qbd("--demo", "--agent", "Claude (Claude Code)", "--session=cc-1", "status")[0], 0)
        with mock.patch("qbd_agent_kit.cli.calling_program", return_value="agy.exe"):
            self.assertEqual(self.run_qbd("--demo", "status")[0], 0)
        gemini, claude, unnamed = self.calls()
        self.assertEqual((gemini["agent"], gemini["session"], gemini["session_title"]), ("Gemini (agy)", "agy-0831", "Morning check: who owes us money"))
        self.assertEqual(gemini["company_file"], "C:\\Users\\Public\\Documents\\Intuit\\QuickBooks\\Company Files\\Bayside Hardware (sample).qbw")
        self.assertIn("Bayside Hardware", gemini["company"], "daily-brief reads the company's name")
        self.assertEqual(gemini["settings"]["writes"], True, "the sample company allows changes")
        self.assertEqual((claude["agent"], claude["session"]), ("Claude (Claude Code)", "cc-1"), "options come before the settings")
        self.assertNotIn("program", claude)
        self.assertEqual(unnamed["program"], "agy.exe")
        self.assertNotIn("agent", unnamed)
        self.assertEqual(list(gemini)[:2], ["time", "user"])

    def test_settings_say_what_a_command_may_change(self):
        self.assertEqual(load_config({}).settings(), {"writes": False})
        on = load_config({"QBD_ALLOW_WRITES": "true", "QBD_WRITE_ENTITIES": "bill,deposit", "QBD_MAX_AMOUNT": "500", "QBD_MASK_PERSONAL_DATA": "false"})
        self.assertEqual(on.settings(), {"writes": True, "deletes": False, "entities": ["bill", "deposit"], "max_amount": 500, "mask": False})
        self.assertEqual(load_config({"QBD_STATE_DIR": "shared"}).state_dir, os.path.abspath("shared"))
        self.assertEqual(load_config({"QBD_AGENT": " x " * 40}).agent[-1], "…")

    def test_the_last_facts_read_from_quickbooks_are_kept_per_company_file(self):
        ctx = cli.make_context(load_config(demo=True))
        from qbd_agent_kit.tools.common import company_facts

        company_facts(ctx)
        cli.remember(ctx, ctx.config)
        with open(os.path.join(self.folder, "quickbooks-demo.json"), encoding="utf-8") as f:
            data = json.load(f)
        entry = data["files"][data["last"]]
        self.assertEqual(entry["closing_date"], company_facts(ctx).closing_date)
        self.assertIn("sample company", entry["product"])
        self.assertTrue(entry["read"].endswith("Z"))


class Line(Base):
    def waiter(self, pid: int, queued_ns: int) -> str:
        path = os.path.join(self.folder, "live", f"{pid}.json")
        live.write_json(path, {"pid": pid, "state": "waiting", "started": live._now(), "queued_ns": queued_ns})
        return path

    def test_commands_take_their_turn_in_the_order_they_asked(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        time.sleep(0.2)
        first = self.waiter(other.pid, time.time_ns() - 10 ** 9)  # asked a second before us
        me = live.Live(self.folder, {"command": "status"})
        got = threading.Event()

        def wait_turn():
            with live.take_turn(self.folder, me, 5):
                got.set()

        t = threading.Thread(target=wait_turn)
        t.start()
        time.sleep(0.5)
        self.assertFalse(got.is_set(), "the lock is free, but the earlier waiter goes first")
        with open(me.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["state"], "waiting")
        os.remove(first)  # it took its turn
        t.join(5)
        self.assertTrue(got.is_set())
        self.assertEqual(me.info["state"], "running")

    def test_waiters_left_over_from_ended_commands_are_skipped(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.waiter(dead.pid, time.time_ns() - 10 ** 9)
        self.waiter(os.getpid(), time.time_ns() - 120 * 10 ** 9)  # running, but asked two minutes ago: it gave up long since
        me = live.Live(self.folder, {"command": "status"})
        started = time.monotonic()
        with live.take_turn(self.folder, me, 5):
            pass
        self.assertLess(time.monotonic() - started, 1)

    @unittest.skipIf(sys.platform == "win32", "uses flock")
    def test_a_command_gives_up_after_its_wait_and_says_busy(self):
        import fcntl

        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "quickbooks.lock"), "a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            me = live.Live(self.folder, {"command": "status"})
            with self.assertRaises(QbConnectionError) as e:
                with live.take_turn(self.folder, me, 0.3):
                    pass
        self.assertEqual(e.exception.code, "busy")
        self.assertEqual(me.info["state"], "gave_up")


class Progress(Base):
    class Watch(live.Live):
        def __init__(self, folder):
            super().__init__(folder, {"command": "find-duplicates"})
            self.seen = []

        def update(self, **changes):
            super().update(**changes)
            if "step" in changes:
                self.seen.append(dict(changes["step"]))

    def test_the_live_file_follows_paged_reads_and_reports(self):
        qb = QbClient(DemoTransport("2026-10-20"))
        qb.watch = watch = self.Watch(self.folder)
        rows = qb.query("TransactionQueryRq", {}, 90, page_size=40).rows
        self.assertEqual(len(rows), 90)
        pages = [s for s in watch.seen if s["request"] == "TransactionQueryRq" and "left" in s]
        self.assertEqual([(s["page"], s["read"]) for s in pages], [(1, 40), (2, 40), (2, 80), (3, 80), (3, 90)])
        self.assertTrue(all(s["page_size"] == 40 and s["cap"] == 90 for s in pages))
        self.assertGreater(pages[0]["left"], 0, "after the first page QuickBooks says how many are left")
        host = next(s for s in watch.seen if s["request"] == "HostQueryRq")
        self.assertNotIn("page", host, "the version check is not part of the paged read")
        qb.report("AgingReportQueryRq", {"AgingReportType": "ARAgingDetail"})
        self.assertEqual(watch.seen[-1]["report"], "ARAgingDetail")
        with open(watch.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["company_file"], qb.company_file)
        watch.end()
        self.assertFalse(os.path.exists(watch.path))

    def test_a_stop_asked_in_the_monitor_ends_the_command_before_its_next_request(self):
        qb = QbClient(DemoTransport("2026-10-20"))
        qb.watch = watch = self.Watch(self.folder)
        qb.query("CustomerQueryRq", {}, 5)
        live.ask_stop(self.folder, os.getpid(), "frontdesk@BAYSIDE-PC")
        with self.assertRaises(Stopped) as e:
            qb.query("TransactionQueryRq", {}, 90, page_size=40)
        self.assertNotIsInstance(e.exception, Exception, "a workflow that carries on past a failed step does not catch it")
        self.assertEqual(e.exception.steps, 2)
        self.assertIn("Stopped by frontdesk@BAYSIDE-PC in the Activity Monitor after 2 steps", str(e.exception))
        self.assertEqual(qb.calls[-1]["request"], "CustomerQueryRq", "the stopped request was not sent")
        watch.stoppable = False
        qb.query("VendorQueryRq", {}, 5)  # a single commit is not stopped halfway
        watch.end()
        self.assertEqual(os.listdir(os.path.join(self.folder, "live")), [])

    def test_a_stopped_command_fails_with_stopped_and_a_paused_session_waits_for_resume(self):
        key = live.who("Claude (Claude Code)", "cc-1", "")
        live.pause(self.folder, key, "frontdesk@BAYSIDE-PC", stopped="Find duplicate entries")
        # status runs no further than the pause check: on the QuickBooks PC it would ask QuickBooks
        with mock.patch.dict(os.environ, {"QBD_AGENT": "Claude (Claude Code)", "QBD_SESSION": "cc-1"}), \
                mock.patch.object(cli, "run_tool", return_value={}):
            code, out = self.run_qbd("status")
            self.assertEqual((code, out["error"]["code"]), (3, "paused"))
            self.assertIn("frontdesk@BAYSIDE-PC paused this session", out["error"]["message"])
            self.assertIn("Resume", out["error"]["hint"])
            self.assertEqual(self.run_qbd("--session", "cc-2", "status"), (0, {}), "another session of the same assistant goes on")
            live.resume(self.folder, key)
            self.assertEqual(self.run_qbd("status"), (0, {}))
        self.assertEqual([c["outcome"] for c in self.calls("calls.jsonl")][0], "paused")
        self.assertFalse(os.listdir(os.path.join(self.folder, "live")), "no live file is left behind")
        self.assertEqual(cli.failure(Stopped("frontdesk", 3), "find-duplicates")[0]["error"]["code"], "stopped")


class Approvals(Base):
    def draft(self, **env: str) -> str:
        with mock.patch.dict(os.environ, {"QBD_AGENT": "Claude (Claude Code)", "QBD_SESSION": "cc-7f3c", **env}):
            code, out = self.run_qbd("--demo", "draft", "add", "customer", "--fields", '{"Name": "ZZ-TEST Monitor"}', "--reason", "test")
        self.assertEqual(code, 0, out)
        return out["token"]

    def test_a_draft_keeps_who_asked_its_checks_and_warnings(self):
        token = self.draft()
        with open(os.path.join(self.folder, "drafts", f"{token}.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual((d["agent"], d["session"], d["settings"]["writes"]), ("Claude (Claude Code)", "cc-7f3c", True))
        self.assertIn('No customer named "ZZ-TEST Monitor" yet', d["checks"])
        self.assertLessEqual(abs(d["created"] - (d["expires"] - 15 * 60)), 1)
        with mock.patch.dict(os.environ, {"QBD_AGENT": "Claude (Claude Code)"}):
            fields = '{"VendorRef": "Coastal Freight Lines", "TxnDate": "2026-10-21", "RefNumber": "CF-1", "ExpenseLineAdd": [{"AccountRef": "Freight and Delivery", "Amount": 120}]}'
            code, out = self.run_qbd("--demo", "draft", "add", "bill", "--fields", fields)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.folder, "drafts", f"{out['token']}.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["total"], 120)
        for check in ("Under the limit for changes", "Dated after the closing date", "The vendor and account names it uses exist",
                      "Number CF-1 not used for Coastal Freight Lines"):
            self.assertTrue(any(c.startswith(check) for c in d["checks"]), (check, d["checks"]))

    def test_approved_in_the_monitor_it_is_saved_once_and_the_assistant_learns_what_was_saved(self):
        token = self.draft()
        code, saved = self.run_qbd("--demo", "--agent", "Activity Monitor", "commit", token, "--approved-in", "monitor")
        self.assertEqual(code, 0, saved)
        code, later = self.run_qbd("--demo", "commit", token)
        self.assertEqual(code, 0, later)
        self.assertEqual((later["approved_in"], later["ids"]), ("monitor", saved["ids"]))
        self.assertIn("approved this change in the Activity Monitor", later["note"])
        with open(os.path.join(self.folder, "audit-demo.jsonl"), encoding="utf-8") as f:
            log = [json.loads(x) for x in f]
        self.assertEqual(len(log), 1, "saved once")
        self.assertEqual((log[0]["approved_in"], log[0]["agent"], log[0]["session"]), ("monitor", "Claude (Claude Code)", "cc-7f3c"))

    def test_rejected_in_the_monitor_nothing_is_saved_and_the_assistant_is_told(self):
        token = self.draft()
        config = load_config(demo=True)
        entry = reject(config, Drafts(os.path.join(self.folder, "drafts")), token)
        self.assertEqual(entry["outcome"], "rejected")
        with self.assertRaisesRegex(ToolError, "gone"):
            reject(config, Drafts(os.path.join(self.folder, "drafts")), token)
        totals = self.run_qbd("--demo", "activity")[1]["totals"]
        self.assertEqual((totals["changes_saved"], totals["needs_a_look"]), (0, 0), "a rejected change is no problem")
        code, out = self.run_qbd("--demo", "commit", token)
        self.assertEqual(code, 2)
        self.assertIn("rejected this change in the Activity Monitor", out["error"]["message"])

    def test_a_pc_can_ask_for_approval_in_the_monitor(self):
        token = self.draft()
        with mock.patch.dict(os.environ, {"QBD_REQUIRE_MONITOR_APPROVAL": "true"}):
            code, out = self.run_qbd("--demo", "commit", token)
            self.assertEqual(code, 2)
            self.assertIn("approved in the Activity Monitor", out["error"]["message"])
            self.assertEqual(self.run_qbd("--demo", "commit", token, "--approved-in", "monitor")[0], 0)
            code, out = self.run_qbd("--demo", "commit", token)
            self.assertEqual((code, out["approved_in"]), (0, "monitor"))

    def test_a_draft_changed_after_it_was_shown_is_not_saved(self):
        token = self.draft()
        drafts = Drafts(os.path.join(self.folder, "drafts"))
        seen = drafts.fingerprint(token)
        path = drafts._path(token)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["request"] = d["request"].replace("ZZ-TEST Monitor", "ZZ-TEST Changed")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        code, out = self.run_qbd("--demo", "commit", token, "--approved-in", "monitor", "--seen", seen)
        self.assertEqual(code, 2, out)
        self.assertIn("changed after it was shown", out["error"]["message"])
        self.assertTrue(os.path.exists(path), "the draft waits to be looked at again")
        code, out = self.run_qbd("--demo", "commit", token, "--approved-in", "monitor", "--seen", drafts.fingerprint(token))
        self.assertEqual(code, 0, out)

    def test_a_draft_is_read_only_under_its_own_token(self):
        drafts = Drafts(os.path.join(self.folder, "drafts"))
        os.makedirs(drafts.folder)
        kept = os.path.join(self.folder, "keep.json")
        with open(kept, "w", encoding="utf-8") as f:
            f.write("{}")
        with open(drafts._path("0badc0de"), "w", encoding="utf-8") as f:
            json.dump({"token": "../keep", "action": "add", "entity": "customer", "request": "", "message": "", "summary": "",
                       "reason": "", "company_file": "", "expires": 1}, f)
        self.assertIsNone(drafts._load("0badc0de"))
        self.assertIsNone(drafts._load("../keep"))
        self.draft()  # a new draft clears out old files
        self.assertTrue(os.path.exists(kept), "nothing outside the drafts folder is touched")
        self.assertFalse(os.path.exists(drafts._path("0badc0de")))

    def test_an_import_accounts_for_every_record_and_expires_as_one(self):
        path = os.path.join(self.home.name, "customers.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Name\nZZ-TEST One\nZZ-TEST Two\n")
        drafts = Drafts(os.path.join(self.folder, "drafts"))
        code, out = self.run_qbd("--demo", "import", "customer", "--csv", path)
        self.assertEqual(code, 0, out)
        first, second = batch_tokens(drafts._load(out["token"]).request)
        seen = drafts.fingerprint(out["token"])
        os.remove(drafts._path(second))
        self.assertNotEqual(drafts.fingerprint(out["token"]), seen, "a record's draft is part of what was shown")
        code, saved = self.run_qbd("--demo", "commit", out["token"])
        self.assertEqual(code, 0, saved)
        self.assertEqual([r["outcome"] for r in saved["results"]], ["done", "not_sent"])
        self.assertIn("gone", saved["results"][1]["error"])

        code, out = self.run_qbd("--demo", "import", "customer", "--csv", path)
        self.assertEqual((code, out["counts"]["ready"]), (0, 1), "the saved record is skipped")
        drafts.retire(drafts._load(out["token"]))
        self.assertEqual(os.listdir(drafts.folder), [f"{out['token']}.expired.json"], "an expired import is listed once, without its records")


if __name__ == "__main__":
    unittest.main()
