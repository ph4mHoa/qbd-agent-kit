"""Add-ons: what installs (the catalog's files, others only where an administrator allows them), what a zip may hold,
removing one, and starting one with its folder on the path."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from qbd_agent_kit import __version__, addons, api, cli
from qbd_agent_kit.addons import Policy, this_build
from qbd_agent_kit.addons.__main__ import main as launcher

URL = "https://example.com/hello-0.1.0.zip"
READ_POLICY = addons.policy  # the tests set the policy; this one reads it


def addon_zip(folder: str, file: str = "hello.zip", extra: dict | None = None, **about: object) -> tuple[str, str]:
    """A small add-on: its package writes the arguments it was started with to the file named in HELLO_OUT."""
    m = {"id": "hello", "name": "Hello", "version": "0.1.0", "publisher": "Tests", "description": "Says hello.", "api": 1,
         "module": "hello_addon", "window": True, "build": "any", **about}
    path = os.path.join(folder, file)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("addon.json", json.dumps(m))
        z.writestr("hello_addon/__init__.py", "")
        z.writestr("hello_addon/__main__.py", "import os, sys, json\nimport vendored\n"
                   "with open(os.environ['HELLO_OUT'], 'w') as f:\n    f.write(json.dumps([sys.argv[1:], vendored.WORD]))\n")
        z.writestr("vendor/vendored.py", "WORD = 'hello'\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    with open(path, "rb") as f:
        return path, hashlib.sha256(f.read()).hexdigest()


class Addons(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.dl = tempfile.TemporaryDirectory()
        self.addCleanup(self.dl.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("QBD_")}
        for p in (mock.patch.dict(os.environ, {**env, "LOCALAPPDATA": self.home.name}, clear=True),
                  mock.patch.object(addons, "policy", lambda: self.policy)):
            p.start()
            self.addCleanup(p.stop)
        self.policy = Policy()
        self.zip, self.sha = addon_zip(self.dl.name)
        self.catalog = {"hello": {"name": "Hello", "publisher": "Tests", "description": "Says hello.", "version": "0.1.0",
                                  "builds": {this_build(): {"url": URL, "sha256": self.sha, "size": os.path.getsize(self.zip)}}}}
        patch = mock.patch.object(addons, "catalog", lambda: self.catalog)
        patch.start()
        self.addCleanup(patch.stop)
        self.folder = os.path.join(self.home.name, "qbd-agent-kit", "addons")

    def qbd(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["addons", *argv])
        return code, json.loads(out.getvalue())

    def served(self, path: str):
        def urlopen(url: str, timeout: float):
            self.assertEqual(url, URL)
            return open(path, "rb")
        return mock.patch("urllib.request.urlopen", urlopen)

    def test_the_catalogs_add_on_is_downloaded_checked_and_installed_once_asked(self):
        code, out = self.qbd()
        self.assertEqual((code, [(r["id"], r["latest"], r["installed"]) for r in out["addons"]]), (0, [("hello", "0.1.0", None)]))
        code, out = self.qbd("install", "hello")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["plan"]["from"], out["plan"]["sha256"], out["plan"]["from_catalog"]), (URL, self.sha, True))
        self.assertFalse(os.path.exists(self.folder), "nothing installed without --yes")
        with self.served(self.zip):
            code, out = self.qbd("install", "hello", "--yes")
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.folder, "hello", addons.RECORD), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual((record["sha256"], record["official"], record["from"]), (self.sha, True, URL))
        self.assertEqual(sorted(os.listdir(self.folder)), ["hello"], "no download or temporary folder left")
        self.assertEqual(self.qbd()[1]["addons"][0]["installed"], "0.1.0")
        with open(os.path.join(self.home.name, "qbd-agent-kit", "calls.jsonl"), encoding="utf-8") as f:
            logged = [json.loads(x) for x in f]
        self.assertEqual([(e["command"], e["args"]) for e in logged][-2], ("addons", {"action": "install", "addon": "hello", "yes": True}))

    def test_a_download_that_is_not_the_catalogs_file_is_refused(self):
        other, _ = addon_zip(self.dl.name, "other.zip", description="Says hi.")  # no larger than the catalog's
        with self.served(other):
            code, out = self.qbd("install", "hello", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("SHA-256 differs", out["error"]["message"])
        self.assertEqual(os.listdir(self.folder), [], "the download is deleted")

    def test_a_zip_installs_when_the_catalog_names_it_or_an_administrator_allows_it(self):
        code, out = self.qbd("install", self.zip, "--yes")
        self.assertEqual((code, out["installed"]["from_catalog"]), (0, True), out)
        mine, sha = addon_zip(self.dl.name, "mine.zip", id="mine", name="Mine")
        code, out = self.qbd("install", mine, "--yes")
        self.assertEqual(code, 2)
        self.assertIn(sha, out["error"]["message"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "mine")))
        for allowed in ((sha.upper(),), ("*",)):
            self.policy = Policy(allowed=tuple(a.lower() for a in allowed), where="test")
            code, out = self.qbd("install", mine, "--yes")
            self.assertEqual((code, out["installed"]["from_catalog"]), (0, False), out)
        self.policy = Policy()
        with self.assertRaisesRegex(addons.AddonError, "policy does not allow it"):
            addons.check("mine")

    def test_an_administrator_can_turn_add_ons_off(self):
        self.qbd("install", self.zip, "--yes")
        self.policy = Policy(off=True, where="test")
        code, out = self.qbd()
        self.assertEqual((code, out["policy"]), (0, "Add-ons are turned off by an administrator (test)."))
        for argv in (("install", self.zip, "--yes"), ("run", "hello")):
            code, out = self.qbd(*argv)
            self.assertEqual(code, 2)
            self.assertIn("turned off", out["error"]["message"])

    def test_a_zip_with_a_path_outside_its_folder_or_a_link_is_refused(self):
        bad = ["../evil.py", "/evil.py", "a/../../evil.py", "a\\..\\evil.py", "C:/evil.py", addons.RECORD]
        self.policy = Policy(allowed=("*",), where="test")
        for name in bad:
            path, _ = addon_zip(self.dl.name, "bad.zip", extra={name: "x"})
            code, out = self.qbd("install", path, "--yes")
            self.assertEqual(code, 2, name)
            self.assertIn("does not unpack", out["error"]["message"])
        path = os.path.join(self.dl.name, "link.zip")
        with zipfile.ZipFile(path, "w") as z:
            info = zipfile.ZipInfo("addon.json")
            info.external_attr = (0o120777 << 16)  # a symbolic link
            z.writestr(info, "/etc/passwd")
        self.assertEqual(self.qbd("install", path, "--yes")[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.dl.name, "evil.py")))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "hello")))

    def test_an_add_on_for_another_api_or_python_is_refused(self):
        self.policy = Policy(allowed=("*",), where="test")
        for about, words in (({"api": 2}, "Update qbd"), ({"build": "cp27-win32"}, "built for cp27-win32"),
                             ({"id": "../x"}, "id is missing or not valid")):
            path, _ = addon_zip(self.dl.name, "odd.zip", **about)
            code, out = self.qbd("install", path, "--yes")
            self.assertEqual(code, 2, about)
            self.assertIn(words, out["error"]["message"])

    def test_removing_asks_first_then_deletes_the_folder(self):
        self.qbd("install", self.zip, "--yes")
        code, out = self.qbd("remove", "hello")
        self.assertEqual((code, out["plan"]["name"]), (0, "Hello"))
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "hello")))
        code, out = self.qbd("remove", "hello", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.folder), [])
        self.assertEqual(self.qbd("remove", "hello", "--yes")[0], 2)

    def test_run_starts_the_add_on_in_a_process_of_its_own(self):
        self.qbd("install", self.zip, "--yes")
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            code, out = self.qbd("run", "hello")
        self.assertEqual((code, out), (0, {"started": "hello", "pid": 4242}))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["-m", "qbd_agent_kit.addons", "hello"])
        self.assertEqual(popen.call_args.kwargs["cwd"], addons.KIT_HOME)

    def test_the_launcher_runs_the_add_on_with_its_vendor_folder(self):
        self.qbd("install", self.zip, "--yes")
        out = os.path.join(self.dl.name, "out.json")
        path, modules, argv = list(sys.path), dict(sys.modules), list(sys.argv)
        try:
            with mock.patch.dict(os.environ, {"HELLO_OUT": out}):
                launcher(["hello", "--tray"])
        finally:
            sys.path[:], sys.argv[:] = path, argv
            for name in set(sys.modules) - set(modules):
                del sys.modules[name]
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [["--tray"], "hello"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(launcher(["nothing"]), 2)
        self.assertIn("No add-on named 'nothing'", err.getvalue())

    def test_the_menu_installs_and_removes_as_qbd_addons_does_so_the_call_log_keeps_it(self):
        # add-ons.cmd's menu, for people who double-click it: the number, y to install, Enter not to start it, Enter to
        # close; then the number, r and y to remove it
        with self.served(self.zip), mock.patch("builtins.input", side_effect=["1", "y", "", ""]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launcher([]), 0)
        self.assertIn("Installed", out.getvalue())
        self.assertEqual(self.qbd()[1]["addons"][0]["installed"], "0.1.0")
        with mock.patch("builtins.input", side_effect=["1", "r", "y", ""]), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launcher([]), 0)
        self.assertIn("Removed", out.getvalue())
        with open(os.path.join(self.home.name, "qbd-agent-kit", "calls.jsonl"), encoding="utf-8") as f:
            done = [(e["args"]["action"], e["outcome"]) for e in map(json.loads, f) if e["command"] == "addons" and e["args"].get("yes")]
        self.assertEqual(done, [("install", "ok"), ("remove", "ok")])

    def test_the_policy_is_read_from_the_administrators_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with mock.patch.object(addons, "POLICY_FILE", path), mock.patch.object(sys, "platform", "linux"), \
                    mock.patch.object(addons, "policy", READ_POLICY):
                self.assertEqual(addons.policy(), Policy())
                for written, off, allowed in (({"AllowedAddons": ["ABC", " def "]}, False, ("abc", "def")),
                                              ({"AllowedAddons": "abc, def"}, False, ("abc", "def")),
                                              ({"DisableAddons": 1}, True, ()), ("{not json", True, ())):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(written if isinstance(written, str) else json.dumps(written))
                    self.assertEqual((addons.policy().off, addons.policy().allowed), (off, allowed), written)


class Build(unittest.TestCase):
    """scripts/build-addon.py: the zip the catalog names, with only the part of each wheel the add-on uses."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("build_addon", os.path.join(root, "scripts", "build-addon.py"))
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = self.tmp / "hello"
        m = {"id": "hello", "name": "Hello", "version": "0.1.0", "publisher": "Tests", "description": "Says hello.", "api": 1,
             "module": "hello_addon", "window": True, "build": "any", "icon": "hello.ico"}
        for name, text in (("addon.json", json.dumps(m)), ("hello.ico", "ico"), ("hello_addon/__init__.py", ""), ("hello_addon/ui/icon.png", "png"),
                           ("hello_addon/__pycache__/ui.cpython-315.pyc", ""), ("tests/test_hello.py", "")):
            (self.src / name).parent.mkdir(parents=True, exist_ok=True)
            (self.src / name).write_text(text)
        self.catalog = self.tmp / "catalog.json"
        self.catalog.write_text(json.dumps({"hello": {"version": "0.0.9", "builds": {"cp314-win_amd64": {}}}}))
        patch = mock.patch.object(self.build, "CATALOG", self.catalog)
        patch.start()
        self.addCleanup(patch.stop)

    def wheel(self, name: str, files: list[str]) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as z:
            for f in files:
                z.writestr(f, f)
        return path

    def test_the_zip_holds_the_add_on_and_the_part_of_wx_it_uses_and_its_line_goes_in_the_catalog(self):
        kept = ["wx/__init__.py", "wx/__version__.py", "wx/core.py", "wx/adv.py", "wx/_core.cp315-win_amd64.pyd", "wx/_adv.cp315-win_amd64.pyd",
                "wx/siplib.cp315-win_amd64.pyd", "wx/wxbase334u_vc140_x64.dll", "wx/wxbase334u_net_vc140_x64.dll", "wx/wxmsw334u_core_vc140_x64.dll",
                "wxpython-4.3.2.dist-info/METADATA", "wxpython-4.3.2.dist-info/licenses/LICENSE.txt"]
        dropped = ["wx/html.py", "wx/_html.cp315-win_amd64.pyd", "wx/wxmsw334u_adv_vc140_x64.dll", "wx/wxmsw334u_html_vc140_x64.dll",
                   "wx/core.pyi", "wx/locale/de/LC_MESSAGES/wxstd.mo", "wx/lib/agw/__init__.py", "wx/libcairo-2.dll", "wxpython-4.3.2.dist-info/RECORD"]
        wheel = self.wheel("wxpython-4.3.2-cp315-cp315-win_amd64.whl", kept + dropped)
        path = self.build.build(self.src, [wheel], self.tmp / "dist")
        self.assertEqual(path.name, "hello-0.1.0-cp315-win_amd64.zip")
        with zipfile.ZipFile(path) as z:
            self.assertEqual(set(z.namelist()), {"addon.json", "hello.ico", "hello_addon/__init__.py", "hello_addon/ui/icon.png",
                                                 *(f"vendor/{k}" for k in kept)})
        self.assertEqual(addons.read_zip(str(path)).build, "cp315-win_amd64")
        data = path.read_bytes()
        self.assertEqual(self.build.build(self.src, [wheel], self.tmp / "again").read_bytes(), data, "the same files make the same zip")
        line = json.loads(self.catalog.read_text())["hello"]
        self.assertEqual((line["version"], line["description"]), ("0.1.0", "Says hello."))
        self.assertEqual(line["builds"], {"cp315-win_amd64": {  # the builds of 0.0.9 are gone
            "url": f"https://github.com/ph4mHoa/qbd-agent-kit/releases/download/v{__version__}/{path.name}",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}})

    def test_a_wheel_it_does_not_know_or_wheels_for_two_pythons_stop_it(self):
        qt = self.wheel("pyside6_essentials-6.12.0-cp310-abi3-win_amd64.whl", [])
        with self.assertRaisesRegex(SystemExit, "knows what to take"):
            self.build.build(self.src, [qt], self.tmp / "dist")
        two = [self.wheel(f"wxpython-4.3.2-{py}-{py}-win_amd64.whl", []) for py in ("cp314", "cp315")]
        with self.assertRaisesRegex(SystemExit, "different Pythons"):
            self.build.build(self.src, two, self.tmp / "dist")


class Api(unittest.TestCase):
    def test_every_name_the_api_lists_is_there(self):
        self.assertEqual(api.API_VERSION, 1)
        self.assertIn(api.API_VERSION, addons.API_VERSIONS)
        for name in api.__all__:
            self.assertTrue(hasattr(api, name), name)


if __name__ == "__main__":
    unittest.main()
