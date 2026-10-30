import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from qbd_agent_kit import __version__, cli


class Cli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("QBD_")}
        patch = mock.patch.dict(os.environ, {**env, "LOCALAPPDATA": self.home.name}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_qbd(self, *argv: str, stdin: str = "") -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def answer(self, *argv: str, stdin: str = "") -> dict:
        code, out = self.run_qbd(*argv, stdin=stdin)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("\n"), 1, "one line of JSON")
        return json.loads(out)

    def error(self, *argv: str, code: int) -> dict:
        got, out = self.run_qbd(*argv)
        self.assertEqual(got, code, out)
        return json.loads(out)["error"]

    def test_version_and_help(self):
        self.assertEqual(self.run_qbd("--version"), (0, f"{__version__}\n"))
        code, out = self.run_qbd("help")
        self.assertEqual(code, 0)
        for cmd in [*cli.COMMANDS, *cli.SETUP]:
            self.assertIn(f"qbd {cmd}", out)
        code, out = self.run_qbd("search", "--help")
        self.assertIn("--ref-number TEXT", out)
        self.assertIn("default 25", out)

    def test_the_plugin_has_the_package_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "plugin", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], __version__)

    def test_the_commands_reference_matches_the_code(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("gen_docs", os.path.join(root, "scripts", "gen-docs.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        with open(os.path.join(root, "docs", "commands.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), gen.render(), "docs/commands.md is out of date: run python scripts/gen-docs.py")

    def zip_inputs(self):
        """The Windows zip's build script, and fake downloads for it."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("build_zip", os.path.join(root, "scripts", "build-windows-zip.py"))
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        src = os.path.join(self.home.name, "dl")
        os.makedirs(src)
        python_zip = os.path.join(src, "python-3.15.0-embed-amd64.zip")
        with zipfile.ZipFile(python_zip, "w") as z:
            z.writestr("python.exe", "")
            z.writestr("python315._pth", "python315.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n")
        wheel = os.path.join(src, "pywin32-312-cp315-cp315-win_amd64.whl")
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("pywin32.pth", "win32\nwin32\\lib\nPythonwin\nimport pywin32_bootstrap\n")
            z.writestr("win32com/__init__.py", "")
        return build, Path(python_zip), Path(wheel), Path(self.home.name, "dist")

    def test_the_windows_zip_holds_python_pywin32_and_the_kit_at_its_root(self):
        build, python_zip, wheel, out = self.zip_inputs()
        path = build.build(python_zip, wheel, out)
        self.assertEqual(path.name, f"qbd-agent-kit-{__version__}-windows-x64.zip")
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            pth = z.read("python/python315._pth").decode()
            launcher = z.read("qbd.cmd")
            shortcut = z.read("what-the-ai-did.cmd")
            addons = z.read("add-ons.cmd")
        for name in ("qbd", "README.txt", "LICENSE.txt", "python/Lib/site-packages/pywin32.pth", "python/Lib/site-packages/win32com/__init__.py",
                     "python/Lib/site-packages/qbd_agent_kit/cli.py", "python/Lib/site-packages/qbd_agent_kit/qbxml/schema.json",
                     "python/Lib/site-packages/qbd_agent_kit/addons/catalog.json"):
            self.assertIn(name, names)
        self.assertFalse([n for n in names if "__pycache__" in n])
        self.assertNotIn("activity-monitor.cmd", names)
        self.assertEqual(pth.split("\r\n")[-3:], ["Lib\\site-packages", "import site", ""])
        self.assertNotIn("#import site", pth)
        self.assertEqual(launcher, b'@"%~dp0python\\python.exe" -m qbd_agent_kit %*\r\n')
        self.assertEqual(shortcut, b'@"%~dp0python\\python.exe" -m qbd_agent_kit activity --open --limit 1 || pause\r\n')
        self.assertEqual(addons, b'@"%~dp0python\\python.exe" -m qbd_agent_kit.addons || pause\r\n')
        with self.assertRaises(SystemExit):
            build.build(python_zip, wheel.with_name("pywin32-312-cp315-cp315-win32.whl"), out)

    def test_docs_and_skills_name_only_real_commands_and_options(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = [os.path.join(root, "README.md"), os.path.join(root, "PRIVACY.md"), os.path.join(root, "plugin", "README.md")]
        paths += [os.path.join(root, "docs", f) for f in os.listdir(os.path.join(root, "docs")) if f != "commands.md"]
        paths += [os.path.join(root, "plugin", "skills", d, "SKILL.md") for d in os.listdir(os.path.join(root, "plugin", "skills"))]
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            code = re.findall(r"```[^\n]*\n(.*?)```", text, re.S) + re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", text, flags=re.S))
            for span in code:
                for cmd, rest in re.findall(r"\bqbd ([a-z0-9][a-z0-9-]*)([^\n]*)", span):
                    if cmd == "help":
                        continue
                    self.assertIn(cmd, [*cli.COMMANDS, *cli.SETUP], f"{path}: qbd {cmd}")
                    for opt in re.findall(r"(?<![\w-])--[a-z][a-z-]*", rest.split(" qbd ")[0]):
                        self.assertTrue(opt in ("--demo", "--pretty", "--out", "--version", "--help") or opt in cli.help_text(cmd), f"{path}: qbd {cmd} {opt}")

    def test_commands_print_one_line_of_json(self):
        r = self.answer("--demo", "receivables", "--top", "1", "--invoices-per-customer=0")
        self.assertEqual(len(r["customers"]), 1)
        self.assertEqual(r["customers"][0]["invoices"], [])
        r = self.answer("--demo", "search", "invoice", "--status", "open", "--limit", "2", "--fields", "Memo,PONumber", "--include-lines=false")
        self.assertEqual(r["count"], 2)
        r = self.answer("--demo", "find-duplicates", "--include", "bills", "--include", "names")
        self.assertTrue(r["findings"])
        self.assertIn("\n  ", self.run_qbd("--demo", "status", "--pretty")[1])

    def test_json_arguments_inline_from_a_file_or_from_stdin(self):
        def customer(body: str, stdin: str = "") -> str:
            return self.answer("--demo", "raw", "CustomerQueryRq", "--body", body, stdin=stdin)["rows"][0]["FullName"]

        self.assertEqual(customer('{"FullName": "Foxhall Landscaping"}'), "Foxhall Landscaping")
        path = os.path.join(self.home.name, "body.json")
        with open(path, "w", encoding="utf-8-sig") as f:  # PowerShell writes a byte order mark
            f.write('{"FullName": "Alder Street Cafe"}')
        self.assertEqual(customer(f"@{path}"), "Alder Street Cafe")
        self.assertEqual(customer("-", stdin='{"FullName": "Juniper Dental Group"}'), "Juniper Dental Group")

    def test_bad_command_lines_exit_2_with_a_hint(self):
        e = self.error("--demo", "search", "invoice", "--limit", "abc", "--from", "2026-13-01", code=2)
        self.assertEqual(e["code"], "invalid_arguments")
        self.assertIn("from: use a date like 2026-10-31", e["message"])
        self.assertIn("limit: Invalid input: expected number, received string", e["message"])
        self.assertEqual(e["hint"], "Run: qbd help search")
        self.assertIs(e["retryable"], False)
        self.assertEqual(self.error("--demo", "search", "invoice", "--bogus", "1", code=2)["message"], "Unknown option --bogus.")
        self.assertEqual(self.error("--demo", "search", "invoice", "--limit", code=2)["message"], "--limit needs a value.")
        self.assertEqual(self.error("--demo", "status", "extra", code=2)["message"], "Unexpected argument 'extra'.")
        self.assertEqual(self.error("--demo", "nope", code=2)["hint"], "Run: qbd help")
        self.assertIn("must be JSON", self.error("--demo", "raw", "CustomerQueryRq", "--body", "{oops", code=2)["message"])
        self.assertIn("must be JSON", self.error("--demo", "raw", "CustomerQueryRq", "--body", '{"MaxReturned": NaN}', code=2)["message"])
        self.assertEqual(self.error("--demo", "raw", "CustomerQueryRq", "--body", '{"Bad": 1}', code=2)["code"], "invalid_request")
        self.assertEqual(self.error("--demo", "raw", "InvoiceAddRq", code=2)["code"], "refused")

    def test_changes_need_writes_on_and_a_token_from_an_earlier_command(self):
        with mock.patch.dict(os.environ, {"QBD_ALLOW_WRITES": "false"}):
            self.assertEqual(self.error("--demo", "draft", "add", "customer", code=2)["code"], "refused")
        d = self.answer("--demo", "draft", "add", "customer", "--fields", '{"Name": "ZZ-TEST Cli"}', "--reason", "test")
        self.assertRegex(d["token"], r"^[0-9a-f]{8}$")
        c = self.answer("--demo", "commit", d["token"])
        self.assertIs(c["done"], True)
        self.assertIn("already used", self.error("--demo", "commit", d["token"], code=2)["message"])
        log = self.answer("--demo", "log", "--limit", "1")
        self.assertEqual(log["entries"][0]["reason"], "test")

    def test_out_writes_utf8_and_prints_nothing(self):
        path = os.path.join(self.home.name, "out.json")
        self.assertEqual(self.run_qbd("--demo", "search", "customer", "--limit", "1", "--out", path), (0, ""))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["type"], "customer")

    @unittest.skipIf(sys.platform == "win32", "QuickBooks may be installed")
    def test_quickbooks_is_only_reached_on_windows(self):
        e = self.error("status", code=1)
        self.assertEqual(e["code"], "not-windows")
        self.assertIn("--demo", e["hint"])

    @unittest.skipIf(sys.platform == "win32", "uses flock")
    def test_commands_take_turns(self):
        import fcntl

        os.makedirs(os.path.join(self.home.name, "qbd-agent-kit"), exist_ok=True)
        with open(os.path.join(self.home.name, "qbd-agent-kit", "quickbooks.lock"), "a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.dict(os.environ, {"QBD_TIMEOUT_SECONDS": "0.3"}):
                e = self.error("status", code=1)
        self.assertEqual(e["code"], "busy")
        self.assertIs(e["retryable"], True)

    def calls(self) -> list[dict]:
        with open(os.path.join(self.home.name, "qbd-agent-kit", "calls-demo.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def test_every_command_goes_to_the_call_log_with_counts_not_values(self):
        names = [r["FullName"] for r in self.answer("--demo", "search", "customer", "--limit", "3")["rows"]]
        self.error("--demo", "report", "audit_trail", code=2)
        self.error("--demo", "nosuch", "--x", code=2)
        found, refused, unknown = self.calls()
        self.assertEqual((found["command"], found["title"], found["args"], found["outcome"]),
                         ("search", "Search QuickBooks records", {"type": "customer", "limit": 3}, "ok"))
        query = found["requests"][-1]
        self.assertEqual(query["request"], "CustomerQueryRq")
        self.assertGreaterEqual(query["items"], 3)
        self.assertEqual(found["items"], sum(r.get("items", 0) for r in found["requests"]))
        self.assertTrue(all(isinstance(r["ms"], int) for r in found["requests"]))
        self.assertFalse([n for n in names if n in json.dumps(found)], "record values stay out of the call log")
        self.assertEqual(refused["outcome"], "refused")
        self.assertIn("Audit Trail", refused["error"])
        self.assertEqual((unknown["command"], unknown["args"], unknown["outcome"]), ("nosuch", ["--x"], "invalid_arguments"))

    def test_the_call_log_can_be_turned_off_and_keeps_90_days_in_parts(self):
        with mock.patch.dict(os.environ, {"QBD_CALL_LOG": "off"}):
            self.answer("--demo", "search", "customer", "--limit", "1")
        folder = os.path.join(self.home.name, "qbd-agent-kit")
        self.assertFalse(os.path.exists(os.path.join(folder, "calls-demo.jsonl")))
        old = os.path.join(folder, "calls-demo.jsonl.1")  # a part from before 0.3, written 100 days ago
        os.makedirs(folder, exist_ok=True)
        with open(old, "w", encoding="utf-8") as f:
            f.write('{"time": "2026-07-01T10:00:00.000Z", "command": "status"}\n')
        os.utime(old, (time.time() - 100 * 86400,) * 2)
        with mock.patch("qbd_agent_kit.safety.audit.CALL_LOG_ROTATE", 10):
            self.answer("--demo", "search", "customer", "--limit", "1")
            self.answer("--demo", "search", "vendor", "--limit", "1")
        self.assertEqual([c["args"]["type"] for c in self.calls()], ["vendor"])
        parts = [n for n in os.listdir(folder) if n.startswith("calls-demo.jsonl.")]
        self.assertEqual(len(parts), 1, "the 100-day-old part is gone, the new one is kept")
        self.assertRegex(parts[0], r"^calls-demo\.jsonl\.\d{8}-\d{6}$")
        a = self.answer("--demo", "activity", "--days", "90")
        self.assertEqual([c["args"]["type"] for c in a["calls"]], ["vendor", "customer"], "the page reads the older part too")

    def test_the_activity_page_shows_changes_and_problems_in_plain_words_without_scripts(self):
        self.answer("--demo", "search", "customer", "--name", "<script>alert(1)</script>")
        self.error("--demo", "report", "audit_trail", code=2)
        fields = '{"CustomerRef": {"FullName": "Granite Peak Contractors"}, "TotalAmount": 1250, "IsAutoApply": true}'
        token = self.answer("--demo", "draft", "add", "receive_payment", "--fields", fields, "--reason", "Check from Granite Peak")["token"]
        self.answer("--demo", "commit", token)
        with mock.patch("webbrowser.open", return_value=True) as browser:
            a = self.answer("--demo", "activity", "--open")
        self.assertIs(a["opened"], True)
        self.assertEqual(browser.call_args[0][0], Path(a["page"]).resolve().as_uri())
        self.assertEqual({k: a["totals"][k] for k in ("commands", "changes_saved", "needs_a_look")},
                         {"commands": 4, "changes_saved": 1, "needs_a_look": 1})
        self.assertEqual(a["changes"][0]["reason"], "Check from Granite Peak")
        with open(a["page"], encoding="utf-8") as f:
            page = f.read()
        for words in ("What the AI did in QuickBooks", "Saved to QuickBooks", "Previewed, not saved yet", "add · receive payment",
                      "Refused", "Check from Granite Peak", "default-src 'none'", "&lt;script&gt;alert(1)"):
            self.assertIn(words, page)
        for technical in ("<script", "CustomerQueryRq", "TotalAmount", "IsAutoApply"):  # one short line per item, no request details
            self.assertNotIn(technical, page)
        self.assertEqual(set(a["calls"][0]), {"time", "command", "title", "args", "outcome", "ms", "items"})
        self.answer("--demo", "activity")
        self.assertEqual(self.answer("--demo", "activity")["totals"]["commands"], 4, "opening the page is not counted")

    def test_cash_and_receivables_pages_draw_the_answer_without_scripts(self):
        plain = self.answer("--demo", "cash-position")
        with mock.patch("webbrowser.open", return_value=True) as browser:
            a = self.answer("--demo", "cash-position", "--open")
        self.assertEqual(browser.call_args[0][0], Path(a["page"]).resolve().as_uri())
        self.assertEqual({k: v for k, v in a.items() if k not in ("page", "opened")}, plain, "the answer stays the same")
        page = Path(a["page"]).read_text(encoding="utf-8")
        for words in ("Cash for the next 5 weeks", "Count late invoices as paid · +$14,824.75", "See-through",
                      "Lowest point: <b>$174,681.90</b>", "Lowest point: today, <b>$181,372.73</b>",
                      "Summit Lumber Supply · bill INV-7000", "6 late bills ($3,321.50)", "default-src 'none'"):
            self.assertIn(words, page)
        with mock.patch("webbrowser.open", return_value=True):
            r = self.answer("--demo", "receivables", "--open")
        ar = Path(r["page"]).read_text(encoding="utf-8")
        for words in ("Who owes you", "Bayside Hardware &amp; Supply", "Granite Peak Contractors", "116 days late",
                      "final notice; consider holding new orders", "Customers owe $21,672.54.", "takes off $500.00"):
            self.assertIn(words, ar)
        for p in (page, ar):
            self.assertNotIn("<script", p)
            self.assertNotIn("Cascadia", p, "Chrome on Windows 10 1607 hangs drawing it")
        from qbd_agent_kit.tools import pages
        row = pages._customer({"customer": "<b>Evil</b>", "balance": 1.0, "overdue": 0, "oldest": 0, "next_step": "not due yet", "credit": 0,
                               "docs": [{"type": "Invoice", "ref": "<i>", "due": "2026-01-02", "left": 1.0}]}, 1.0, "$", "2026-01-01")
        self.assertIn("&lt;b&gt;Evil", row)
        self.assertNotIn("<b>Evil", row)
        self.assertNotIn("<i>", row)


if __name__ == "__main__":
    unittest.main()
