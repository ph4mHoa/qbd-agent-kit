"""Add-ons: programs a person adds to qbd on this PC when they want them, as one adds extensions to a code editor.
The Activity Monitor is the first. An add-on is a zip with an addon.json at its root, its Python package and, under
vendor/, the libraries it needs; qbd unpacks it for the signed-in Windows user and starts it with the kit's own Python.

What installs: an add-on from qbd's catalog, which ships inside qbd and names each download by its SHA-256, so a
changed file is refused. Any other add-on installs only where an administrator allows it, and an administrator can
turn add-ons off (docs/addons.md). Installing runs none of the add-on's code, and qbd's call log keeps a line for each
install and removal. An add-on runs with the rights of the person who starts it, as any program does."""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ToolError
from ..jsutil import dumps
from ..safety.audit import who_am_i

API_VERSIONS = (1,)            # the versions of qbd_agent_kit.api this qbd serves
MANIFEST = "addon.json"
RECORD = ".qbd-install.json"   # qbd's note beside an installed add-on: where it came from, its SHA-256, who installed it
ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?\Z")
NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{0,59}[^\\/:*?\"<>|\x00-\x1f. ]\Z")  # also a file name, for the Start menu
VERSION = re.compile(r"\d+(?:\.\d+){0,3}(?:(?:a|b|rc|\.dev|\.post)\d+)?\Z")
MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)
BUILD = re.compile(r"any|cp\d+-[a-z0-9_]+\Z")
MOST_FILES = 5000
MOST_BYTES = 400 * 2**20       # unpacked
POLICY_KEY = r"SOFTWARE\Policies\qbd-agent-kit"  # under HKEY_LOCAL_MACHINE, where only an administrator can write
POLICY_FILE = "/etc/qbd-agent-kit/policy.json"   # the same values on other systems
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# qbd and its add-ons run from the folder the kit is installed in, so `-m` finds this kit and not a folder of the same
# name elsewhere
KIT_HOME = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AddonError(ToolError):
    """An add-on that cannot be installed, started or removed, and what to do about it."""


@dataclass(frozen=True)
class Manifest:
    """An add-on's addon.json."""
    id: str            # activity-monitor
    name: str          # Activity Monitor
    version: str
    publisher: str
    description: str
    api: int           # the qbd_agent_kit.api version it was made for
    module: str        # the package qbd runs, as python -m runs it
    window: bool       # a window app: started with pythonw.exe, so no console window opens, and listed in the Start menu
    build: str         # cp315-win_amd64: the Python and system its native files are for; "any" when it has none
    icon: str = ""     # an .ico file in the add-on, for its Start menu entry


def manifest(data: Any) -> Manifest:
    """An addon.json's content, checked; AddonError says what is wrong."""
    if not isinstance(data, dict):
        raise AddonError("addon.json must hold a JSON object.")

    def field(key: str, pattern: re.Pattern | None = None, most: int = 400, required: bool = True) -> str:
        v = data.get(key, None if required else "")
        if v == "" and not required:
            return v
        if not isinstance(v, str) or not v.strip() or len(v) > most or (pattern is not None and not pattern.match(v)):
            raise AddonError(f"addon.json: {key} is missing or not valid.")
        return v.strip()

    api = data.get("api")
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise AddonError("addon.json: api is missing or not valid.")
    icon = field("icon", re.compile(r"[\w./-]+\.ico\Z"), 200, required=False)
    if ".." in icon.split("/"):
        raise AddonError("addon.json: icon is missing or not valid.")
    return Manifest(field("id", ID), field("name", NAME), field("version", VERSION), field("publisher", most=60),
                    field("description"), api, field("module", MODULE, 100), data.get("window") is True, field("build", BUILD, 40),
                    icon)


def this_build() -> str:
    """The build of add-on this Python runs: cp315-win_amd64, cp315-win32, cp310-linux_x86_64..."""
    return f"cp{sys.version_info[0]}{sys.version_info[1]}-{re.sub(r'[-.]', '_', sysconfig.get_platform())}"


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v))


def addons_dir(env: Mapping[str, str] | None = None) -> str:
    """Where add-ons are installed: %LOCALAPPDATA%\\qbd-agent-kit\\addons, for the signed-in Windows user alone, also
    when QBD_STATE_DIR shares qbd's state folder with other accounts."""
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA") or env.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "qbd-agent-kit", "addons")


# ---------------------------------------------------------------- the catalog and the administrator's policy


def catalog() -> dict[str, dict]:
    """qbd's catalog: id -> {name, publisher, description, version, builds: {build: {url, sha256, size}}}."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def _build(entry: dict) -> dict | None:
    builds = entry.get("builds") or {}
    return builds.get(this_build()) or builds.get("any")


def _in_catalog(sha256: str) -> str | None:
    """The catalog name of the add-on whose download has this SHA-256."""
    return next((aid for aid, e in catalog().items() for b in (e.get("builds") or {}).values() if b.get("sha256") == sha256), None)


@dataclass(frozen=True)
class Policy:
    off: bool = False                  # DisableAddons: no add-on installs or starts
    allowed: tuple[str, ...] = ()      # AllowedAddons: the SHA-256 of add-on zips allowed besides the catalog's; * = any
    where: str = ""                    # where it was read; "" when no policy is set

    def allows(self, sha256: str) -> bool:
        return "*" in self.allowed or sha256.lower() in self.allowed

    def describe(self) -> str:
        if self.off:
            return f"Add-ons are turned off by an administrator ({self.where})."
        if "*" in self.allowed:
            return f"Any add-on may be installed: an administrator allowed it ({self.where})."
        if self.allowed:
            return f"Add-ons from qbd's catalog, and {len(self.allowed)} more an administrator allowed ({self.where})."
        return "Add-ons from qbd's catalog only" + (f" ({self.where})." if self.where else "; no administrator's policy is set.")


def policy() -> Policy:
    """The administrator's policy for add-ons: the values DisableAddons and AllowedAddons under HKEY_LOCAL_MACHINE\\
    SOFTWARE\\Policies\\qbd-agent-kit on Windows, or in /etc/qbd-agent-kit/policy.json. With none, add-ons from the
    catalog install. A policy that cannot be read turns add-ons off."""
    values: Any = {}
    if sys.platform == "win32":
        import winreg

        where = f"HKEY_LOCAL_MACHINE\\{POLICY_KEY}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY) as k:
                for name in ("DisableAddons", "AllowedAddons"):
                    try:
                        values[name] = winreg.QueryValueEx(k, name)[0]
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            return Policy()
        except OSError:
            return Policy(True, (), f"{where}, unreadable")
    else:
        where = POLICY_FILE
        try:
            with open(POLICY_FILE, encoding="utf-8") as f:
                values = json.load(f)
        except FileNotFoundError:
            return Policy()
        except (OSError, ValueError):
            return Policy(True, (), f"{where}, unreadable")
        if not isinstance(values, dict):
            return Policy(True, (), f"{where}, unreadable")
    allowed = values.get("AllowedAddons") or ()
    if isinstance(allowed, str):
        allowed = re.split(r"[\s,;]+", allowed)
    off = values.get("DisableAddons")
    return Policy(off not in (None, 0, "0", "", "false"), tuple(a.strip().lower() for a in allowed if isinstance(a, str) and a.strip()),
                  where)


# ---------------------------------------------------------------- what is installed


def installed() -> dict[str, tuple[Manifest, dict]]:
    """The add-ons installed for this Windows user: id -> (its addon.json, qbd's record of its install)."""
    out: dict[str, tuple[Manifest, dict]] = {}
    root = addons_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if not ID.match(name):
            continue  # an install or removal under way
        try:
            with open(os.path.join(root, name, MANIFEST), encoding="utf-8") as f:
                m = manifest(json.load(f))
            with open(os.path.join(root, name, RECORD), encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError, AddonError):
            continue
        if m.id == name and isinstance(record, dict):
            out[name] = (m, record)
    return out


def listing() -> dict:
    """qbd addons list: the catalog's add-ons, then the others installed here."""
    have = installed()
    rows = []
    for aid, e in catalog().items():
        b = _build(e)
        m = have.pop(aid, (None, None))[0]
        rows.append({"id": aid, "name": e["name"], "publisher": e["publisher"], "description": e["description"], "from_catalog": True,
                     "latest": e["version"] if b else None, "installed": m.version if m else None,
                     "update": bool(m and b and _version_key(e["version"]) > _version_key(m.version)),
                     **({} if b else {"note": f"No build for this PC ({this_build()}); it is made for the qbd Windows download."})})
    for aid, (m, record) in have.items():
        rows.append({"id": aid, "name": m.name, "publisher": m.publisher, "description": m.description,
                     "from_catalog": bool(record.get("official")), "latest": None, "installed": m.version, "update": False})
    return {"addons": rows, "folder": addons_dir(), "policy": policy().describe()}


def _fits(m: Manifest) -> None:
    if m.api not in API_VERSIONS:
        newer = m.api > API_VERSIONS[-1]
        raise AddonError(f"{m.name} {m.version} is made for version {m.api} of qbd's add-on API, and this qbd serves version "
                         f"{API_VERSIONS[-1]}. {'Update qbd' if newer else f'Install a newer {m.name}'}, then try again.")
    if m.build not in ("any", this_build()):
        raise AddonError(f"{m.name} {m.version} is built for {m.build}, and this qbd runs {this_build()}.")


def check(addon_id: str) -> tuple[Manifest, dict]:
    """An installed add-on that may start now: installed whole, made for this qbd, and still allowed on this PC."""
    rules = policy()
    if rules.off:
        raise AddonError(rules.describe())
    found = installed().get(addon_id)
    if found is None:
        raise AddonError(f"No add-on named {addon_id!r} is installed. Run qbd addons list to see what is.")
    m, record = found
    if not record.get("official") and not rules.allows(str(record.get("sha256") or "")):
        raise AddonError(f"{m.name} is not from qbd's catalog, and this PC's policy does not allow it (any more).")
    _fits(m)
    return m, record


# ---------------------------------------------------------------- installing and removing


def _sha256(path: str) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def read_zip(path: str) -> Manifest:
    """The add-on in a zip, once every path in it is one qbd unpacks: inside the add-on's folder, no links."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MOST_FILES or sum(i.file_size for i in infos) > MOST_BYTES:
                raise AddonError("The zip is too large to be a qbd add-on.")
            for i in infos:
                parts = i.filename.rstrip("/").split("/")
                if ("\\" in i.filename or ":" in i.filename or any(p in ("", ".", "..") for p in parts) or parts[0] == RECORD
                        or stat.S_ISLNK(i.external_attr >> 16)):
                    raise AddonError(f"The zip holds a path qbd does not unpack: {i.filename!r}. It is not a qbd add-on.")
            try:
                return manifest(json.loads(z.read(MANIFEST)))
            except KeyError:
                raise AddonError("The zip has no addon.json at its root, so it is not a qbd add-on.") from None
            except ValueError:
                raise AddonError("addon.json is not valid JSON.") from None
    except (zipfile.BadZipFile, OSError) as e:
        raise AddonError(f"The file could not be read as a zip: {e}.") from None


def download(url: str, size: int, sha256: str, folder: str) -> str:
    """The catalog's file, saved in folder; refused unless it is exactly the file the catalog names."""
    if not url.startswith("https://"):
        raise AddonError(f"The catalog's address for this add-on is not https: {url}")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=folder)
    h, got = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as f, urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - https only, checked above
            while chunk := r.read(1 << 20):
                got += len(chunk)
                if got > size:
                    raise AddonError("The download is larger than qbd's catalog says, so it was stopped and not installed.")
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != sha256:
            raise AddonError("The downloaded file is not the one qbd's catalog names (its SHA-256 differs), so it was not installed. "
                             "Try again later; if it happens again, report it.")
    except (OSError, http.client.HTTPException) as e:  # urllib's errors are OSErrors
        os.remove(path)
        reason = getattr(e, "reason", None) or e
        raise AddonError(f"The download failed ({reason}). Check this PC's internet connection, then try again.") from None
    except BaseException:
        os.remove(path)
        raise
    return path


def _tidy(root: str) -> None:
    """Folders left by an install or removal that could not finish, an hour on."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if name.startswith(".") and os.path.getmtime(path) < time.time() - 3600:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass


def _in_use(name: str) -> AddonError:
    return AddonError(f"{name} is running, or one of its files is open. Quit it (in its tray icon's menu: Quit), then try again.")


def _unpack(path: str, m: Manifest, record: dict) -> str:
    """Unpacks the add-on beside the one it replaces, then swaps the two folders."""
    root = addons_dir()
    os.makedirs(root, exist_ok=True)
    _tidy(root)
    target = os.path.join(root, m.id)
    new = tempfile.mkdtemp(prefix=f".{m.id}-new-", dir=root)
    try:
        with zipfile.ZipFile(path) as z:
            z.extractall(new)
        with open(os.path.join(new, RECORD), "w", encoding="utf-8") as f:
            f.write(dumps(record) + "\n")
        old = os.path.join(root, f".{m.id}-old-{os.getpid()}-{time.time_ns()}")
        if os.path.exists(target):
            try:
                os.rename(target, old)
            except OSError:
                raise _in_use(m.name) from None
        os.rename(new, target)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return target


def install(source: str, yes: bool) -> dict:
    """qbd addons install: a catalog name, downloaded, or the path of a zip. Without yes, only what it would do."""
    rules = policy()
    if rules.off:
        raise AddonError(rules.describe())
    if source.lower().endswith(".zip") or os.sep in source or "/" in source:
        path = os.path.abspath(source)
        if not os.path.isfile(path):
            raise AddonError(f"No file at {path}.")
        sha, size = _sha256(path)
        official = _in_catalog(sha)
        if official is None and not rules.allows(sha):
            raise AddonError("This zip is not an add-on from qbd's catalog (its SHA-256 matches none), so it was not installed. "
                             f"An administrator can allow it on this PC; see docs/addons.md. Its SHA-256: {sha}")
        m = read_zip(path)
        if official is not None and m.id != official:
            raise AddonError(f"The zip says it is {m.id!r}, but qbd's catalog lists this file as {official!r}.")
        _fits(m)
        about = {"name": m.name, "version": m.version, "publisher": m.publisher, "description": m.description}
        addon_id, url = m.id, ""
    else:
        entry = catalog().get(source)
        if entry is None:
            raise AddonError(f"qbd's catalog has no add-on named {source!r}. Run qbd addons list to see them, or give the path "
                             "of a downloaded .zip.")
        build = _build(entry)
        if build is None:
            made = ", ".join(entry.get("builds") or {}) or "none yet"
            raise AddonError(f"{entry['name']} has no build for this PC ({this_build()}); builds: {made}. It is made for the qbd "
                             "Windows download.")
        about = {k: entry[k] for k in ("name", "version", "publisher", "description")}
        addon_id, url, sha, size, official = source, build["url"], build["sha256"].lower(), int(build["size"]), source
    before = installed().get(addon_id)
    plan = {"id": addon_id, **about, "from_catalog": official is not None, "from": url or path, "sha256": sha,
            "size_mb": round(size / 1e6, 1), "folder": os.path.join(addons_dir(), addon_id),
            **({"replaces": before[0].version} if before else {})}
    if not yes:
        return {"plan": plan, "next": "Nothing was installed. To install it, run the same command with --yes."}
    if url:
        path = download(url, size, sha, addons_dir())
        try:
            m = read_zip(path)
            if m.id != addon_id:
                raise AddonError(f"The download says it is {m.id!r}, not {addon_id!r}, so it was not installed.")
            _fits(m)
            folder = _unpack(path, m, _record(m, sha, url, True))
        finally:
            os.remove(path)
    else:
        folder = _unpack(path, m, _record(m, sha, path, official is not None))
    return {"installed": {**plan, "folder": folder, "start_menu": _shortcut(m, True)}}


def _record(m: Manifest, sha: str, source: str, official: bool) -> dict:
    return {"id": m.id, "version": m.version, "sha256": sha, "official": official, "from": source,
            "installed": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "by": who_am_i()}


def remove(addon_id: str, yes: bool) -> dict:
    """qbd addons remove: the add-on's folder, its Start menu entry and its start at sign-in. Without yes, only what
    it would do."""
    root = addons_dir()
    folder = os.path.join(root, addon_id)
    if not ID.match(addon_id) or not os.path.isdir(folder):
        raise AddonError(f"No add-on named {addon_id!r} is installed. Run qbd addons list to see what is.")
    m = installed().get(addon_id, (None, None))[0]
    plan = {"id": addon_id, **({"name": m.name, "version": m.version} if m else {}), "folder": folder}
    if not yes:
        return {"plan": plan, "next": "Nothing was removed. To remove it, run the same command with --yes."}
    gone = os.path.join(root, f".{addon_id}-old-{os.getpid()}-{time.time_ns()}")
    try:
        os.rename(folder, gone)
    except OSError:
        raise _in_use(m.name if m else addon_id) from None
    shutil.rmtree(gone, ignore_errors=True)
    set_start_at_sign_in(addon_id, False)
    if m is not None:
        _shortcut(m, False)
    return {"removed": plan}


# ---------------------------------------------------------------- starting one


def python_exe(window: bool) -> str:
    """This Python; pythonw.exe beside it for a window app on Windows, so no console window opens."""
    exe = sys.executable
    if window and sys.platform == "win32":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def qbd_command() -> list[str]:
    """qbd as an add-on runs it: this Python (python.exe beside pythonw.exe, so qbd's answer can be read). Run it with
    cwd=KIT_HOME."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            exe = console
    return [exe, "-m", "qbd_agent_kit"]


def command(addon_id: str, *args: str, window: bool = True) -> list[str]:
    """The command line that starts an installed add-on, as the Start menu and Windows' sign-in run it."""
    return [python_exe(window), "-m", "qbd_agent_kit.addons", addon_id, *args]


def start(addon_id: str, args: list[str] | tuple[str, ...] = ()) -> int:
    """qbd addons run: starts an installed add-on in a process of its own and returns its process ID."""
    m, _ = check(addon_id)
    win = sys.platform == "win32"
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if win else 0  # type: ignore[attr-defined]
    p = subprocess.Popen(command(addon_id, *args, window=m.window), cwd=KIT_HOME, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=not win)
    return p.pid


def _run_value(addon_id: str) -> str:
    return f"qbd add-on {addon_id}"


def starts_at_sign_in(addon_id: str) -> bool | None:
    """Whether Windows starts the add-on when this user signs in; None where it cannot be told (not Windows)."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, _run_value(addon_id))
            return True
    except OSError:
        return False


def set_start_at_sign_in(addon_id: str, on: bool, *args: str) -> None:
    """Has Windows start the add-on, with these arguments, when this user signs in; or no longer."""
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, _run_value(addon_id), 0, winreg.REG_SZ, subprocess.list2cmdline(command(addon_id, *args)))
        else:
            try:
                winreg.DeleteValue(k, _run_value(addon_id))
            except OSError:
                pass


def _shortcut(m: Manifest, on: bool) -> bool:
    """A window app's entry in the Start menu, in a QBD Agent Kit folder. Whether it was made (or removed)."""
    if sys.platform != "win32" or not m.window or not os.environ.get("APPDATA"):
        return False
    folder = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "QBD Agent Kit")
    path = os.path.join(folder, f"{m.name}.lnk")
    try:
        if not on:
            os.remove(path)
            if not os.listdir(folder):
                os.rmdir(folder)
            return True
        import win32com.client

        os.makedirs(folder, exist_ok=True)
        link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
        exe, *args = command(m.id)
        link.TargetPath = exe
        link.Arguments = subprocess.list2cmdline(args)
        link.WorkingDirectory = KIT_HOME
        link.Description = m.description[:200]
        icon = os.path.join(addons_dir(), m.id, *m.icon.split("/")) if m.icon else ""
        if icon and os.path.exists(icon):
            link.IconLocation = icon
        link.Save()
        return True
    except Exception:  # noqa: BLE001 - the add-on is installed either way; qbd addons run starts it
        return False
