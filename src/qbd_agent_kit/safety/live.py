"""What qbd is doing right now, for the Activity Monitor. Each command that talks to QuickBooks keeps a small file in
the state folder's live folder while it waits for its turn and while it runs, rewritten before and after each
QuickBooks request, and removed when it ends. It holds names, counts and times, never values from QuickBooks.

The same folders carry what a person asks for in the monitor: live/<pid>.stop stops a command after its current
step, and a file in paused/ holds back an assistant's next commands until someone clicks Resume. Commands take their
turn for QuickBooks in the order they asked for it. Nothing here may fail a command: the monitor is only a view."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator

from ..errors import QbConnectionError, Stopped
from ..jsutil import dumps
from .audit import _now

LIVE = "live"
PAUSED = "paused"
POLL = 0.1                 # seconds between looks at the line
WRITE_REQUEST = re.compile(r"(Add|Mod|Del|Void)Rq\b")  # never stopped halfway: a change is sent whole or not at all


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, value: Any) -> bool:
    """Writes the whole file or nothing, so a reader never sees half of it. Windows refuses to replace a file another
    program is reading at that moment, so it tries a few times."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dumps(value))
        for i in range(5):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.02 * (i + 1))
        os.remove(tmp)
    except OSError:
        pass
    return False


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _epoch(iso: Any) -> float | None:
    try:
        return dt.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def process_started(pid: int) -> float | None:
    """When a running process started (time.time()), 0 when it runs but its start cannot be read, or None when there
    is no such process."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0.0 if ctypes.get_last_error() == 5 else None  # another Windows account's process: there, but closed to us
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(h, *map(ctypes.byref, times)):
                return 0.0
            ticks = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime  # 100 ns since 1601
            return ticks / 1e7 - 11644473600
        finally:
            k32.CloseHandle(h)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="utf-8") as f:
            boot = next(int(x.split()[1]) for x in f if x.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError:
            pass
        return 0.0


def alive(info: Any) -> bool:
    """Whether the command a live file describes is still running: its process exists and started before the file,
    so a new process that got the same id after a crash does not count."""
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return False
    started = process_started(info["pid"])
    began = _epoch(info.get("started"))
    return started is not None and (began is None or started <= began + 2)


class Live:
    """This command's live file."""

    def __init__(self, folder: str, info: dict[str, Any]):
        self.dir = os.path.join(folder, LIVE)
        self.pid = os.getpid()
        self.path = os.path.join(self.dir, f"{self.pid}.json")
        self.info: dict[str, Any] = {"pid": self.pid, "state": "starting", "started": _now(), **info}
        self.steps = 0            # QuickBooks requests sent
        self._step_t = 0.0        # when the current one was sent
        self.stoppable = True     # False for a single commit: there is no step after which to stop
        self.name_of: Any = None  # company file -> company name, when qbd has read it before

    def update(self, **changes: Any) -> None:
        self.info.update(changes)
        write_json(self.path, self.info)

    def wait(self) -> None:
        self.update(state="waiting", queued=_now(), queued_ns=time.time_ns())

    def run(self) -> None:
        self.update(state="running", running_since=_now())

    def connected(self, company_file: str) -> None:
        name = self.name_of(company_file) if self.name_of is not None else None
        self.update(company_file=company_file, **({"company": name} if name else {}))

    def stop_asked(self) -> dict | None:
        asked = read_json(self.path[:-5] + ".stop")
        return asked if isinstance(asked, dict) else {} if os.path.exists(self.path[:-5] + ".stop") else None

    def step(self, request: str, detail: dict[str, Any]) -> None:
        """Before each QuickBooks request: stop here if a person asked to, else show the request."""
        if self.stoppable and not WRITE_REQUEST.search(request):
            asked = self.stop_asked()
            if asked is not None:
                self.update(state="stopped")
                raise Stopped(str(asked.get("by") or "someone at this PC"), self.steps)
        if isinstance(self.info.get("step"), dict):
            self._done(self.info["step"])
        self.steps += 1
        self._step_t = time.time()
        self.update(step={"request": request, "n": self.steps, "since": _now(), **detail})

    def _done(self, step: dict[str, Any]) -> None:
        """The steps before this one, for the monitor's list: the pages of one query fold into one step."""
        done = self.info.setdefault("done", [])
        ms = round((time.time() - self._step_t) * 1000)
        last = done[-1] if done else None
        if last is not None and "page" in step and step.get("page", 1) > 1 and last.get("request") == step["request"]:
            last.update(pages=last.get("pages", 1) + 1, ms=last.get("ms", 0) + ms, **({"read": step["read"]} if "read" in step else {}))
            return
        done.append({"request": step["request"], "ms": ms, **{k: step[k] for k in ("report", "read") if k in step}})
        del done[:-20]

    def answered(self, **counts: Any) -> None:
        if isinstance(self.info.get("step"), dict):
            self.update(step={**self.info["step"], **counts})

    def end(self) -> None:
        _remove(self.path)
        _remove(self.path[:-5] + ".stop")


def _try_lock(f: Any) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def waiting(folder: str, within: float) -> list[dict]:
    """The commands waiting for their turn, first in line first. A waiter gives up after `within` seconds, so an older
    waiting file is left over from a command that ended without removing it."""
    d = os.path.join(folder, LIVE)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    now = time.time_ns()
    found = []
    for name in names:
        info = read_json(os.path.join(d, name))
        if (isinstance(info, dict) and info.get("state") == "waiting" and isinstance(info.get("queued_ns"), int)
                and now - info["queued_ns"] < (within + 5) * 1e9 and alive(info)):
            found.append(info)
    return sorted(found, key=lambda i: (i["queued_ns"], i["pid"]))


@contextmanager
def take_turn(folder: str, live: Live, wait: float) -> Iterator[None]:
    """QuickBooks answers one request at a time, so commands from several agents take turns, in the order they asked;
    each waits up to `wait` seconds, then gives up with "busy", which is safe to retry."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "quickbooks.lock"), "a+b") as f:
        live.wait()
        mine = (live.info["queued_ns"], live.pid)
        deadline = time.monotonic() + wait
        while True:
            ahead = [w for w in waiting(folder, wait) if (w["queued_ns"], w["pid"]) < mine]
            if not ahead and _try_lock(f):
                break
            if time.monotonic() > deadline:
                live.update(state="gave_up")
                raise QbConnectionError("busy", "Another qbd command is using QuickBooks right now.", "Run this command again in a moment.")
            time.sleep(POLL)
        live.run()
        yield  # closing the file releases the lock


# ---------------------------------------------------------------- pause and resume, from the monitor


def who(agent: str, session: str, program: str) -> dict[str, str]:
    """What a pause holds back: one assistant's session, told apart by its name, its session and, for an assistant
    that gives no name, the program that started qbd."""
    return {"agent": agent or "", "session": session or "", "program": "" if agent else program or ""}


def pause_path(folder: str, key: dict[str, str]) -> str:
    digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return os.path.join(folder, PAUSED, f"{digest}.json")


def paused(folder: str, key: dict[str, str]) -> dict | None:
    """The pause a person set in the Activity Monitor for this session, if any."""
    info = read_json(pause_path(folder, key))
    return info if isinstance(info, dict) and who(info.get("agent", ""), info.get("session", ""), info.get("program", "")) == key else None


def pause(folder: str, key: dict[str, str], by: str, **about: Any) -> str:
    path = pause_path(folder, key)
    write_json(path, {**key, "by": by, "at": _now(), **about})
    return path


def resume(folder: str, key: dict[str, str]) -> None:
    _remove(pause_path(folder, key))


def ask_stop(folder: str, pid: int, by: str) -> None:
    write_json(os.path.join(folder, LIVE, f"{pid}.stop"), {"by": by, "at": _now()})


# ---------------------------------------------------------------- the last facts read from QuickBooks


def remember_quickbooks(folder: str, demo: bool, company_file: str, facts: dict[str, Any]) -> None:
    """quickbooks.json: per company file, what qbd last read about it and when, for the monitor's Safety panel. The
    monitor never asks QuickBooks itself."""
    path = os.path.join(folder, "quickbooks-demo.json" if demo else "quickbooks.json")
    data = read_json(path)
    files = data.get("files") if isinstance(data, dict) and isinstance(data.get("files"), dict) else {}
    files[company_file] = {**(files.get(company_file) if isinstance(files.get(company_file), dict) else {}), **facts, "read": _now()}
    write_json(path, {"files": files, "last": company_file})


def remembered(folder: str, demo: bool, company_file: str) -> dict:
    data = read_json(os.path.join(folder, "quickbooks-demo.json" if demo else "quickbooks.json"))
    entry = data.get("files", {}).get(company_file) if isinstance(data, dict) and isinstance(data.get("files"), dict) else None
    return entry if isinstance(entry, dict) else {}
