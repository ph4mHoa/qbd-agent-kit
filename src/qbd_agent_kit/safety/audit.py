"""Every write attempt is appended to a local JSON Lines file: who, when, what, and what QuickBooks
answered. Every command goes to a second file, the call log. The files never leave the PC."""
from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import re
import socket
import sys
import time
from typing import Any

from ..jsutil import MISSING, dumps, text
from .privacy import mask_xml


def who_am_i() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return socket.gethostname()


# Shells and launchers between an assistant and qbd: the program that started qbd is the first one above them.
LAUNCHERS = re.compile(r"(cmd|powershell|pwsh|conhost|openconsole|windowsterminal|bash|sh|dash|zsh|fish|wsl|pyw?|"
                       r"python[\d.]*w?|uvx?|env|timeout|nohup|sudo|xargs)(\.exe)?", re.I)
# Reached without a program in between: a person typed the command, or Windows itself started it.
NO_PROGRAM = re.compile(r"(explorer|userinit|winlogon|wininit|services|svchost|taskhostw|sshd|systemd|init|login|tmux.*|screen)(\.exe)?", re.I)


def _windows_processes() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, program file name), from one snapshot of the running processes."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        table = {}
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            table[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return table
    finally:
        k32.CloseHandle(snap)


def _proc_parent(pid: int) -> tuple[int, str] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            name = f.read().strip()
    except OSError:
        return None
    return int(stat[stat.rindex(")") + 2:].split()[1]), name


def calling_program() -> str:
    """The program that started qbd, past the shells and launchers in between: agy.exe, claude.exe, node.exe... The
    call log names it when an assistant gives no name of its own (QBD_AGENT). "" when it cannot be told."""
    try:
        table = _windows_processes() if sys.platform == "win32" else None
        pid, seen = os.getppid(), set()
        while pid and pid not in seen and len(seen) < 12:
            seen.add(pid)
            found = table.get(pid) if table is not None else _proc_parent(pid)
            if found is None:
                return ""
            parent, name = found
            if not LAUNCHERS.fullmatch(name):
                return "" if NO_PROGRAM.fullmatch(name) else name
            pid = parent
    except Exception:  # noqa: BLE001 - only a label
        pass
    return ""


def _now() -> str:
    t = dt.datetime.now(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def write_audit(path: str, entry: dict[str, Any]) -> None:
    """entry: companyFile, action, entity, summary, reason, source, before, outcome (done, failed, unknown), ids, error,
    request."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    request = entry.get("request", MISSING)
    line = {"time": _now(), "user": who_am_i(), **entry, "request": mask_xml(request)[:4000] if isinstance(request, str) else request}
    with open(path, "a", encoding="utf-8") as f:
        f.write(dumps(line) + "\n")


CALL_LOG_ROTATE = 5 * 1024 * 1024  # bytes; past this the call log moves to <name>.<date-time> and a new one starts
LOG_DAYS = 90                       # older parts of the call log are deleted; qbd activity covers 90 days at most
ARCHIVE = re.compile(r"\d{8}-\d{6}|1")  # <name>.20261022-093000, or <name>.1 from before 0.3


def archives(path: str) -> list[str]:
    """The call log's older parts, oldest first."""
    folder, name = os.path.split(os.path.abspath(path))
    try:
        found = [os.path.join(folder, f) for f in os.listdir(folder) if f.startswith(name + ".") and ARCHIVE.fullmatch(f[len(name) + 1:])]
        return sorted(found, key=os.path.getmtime)
    except OSError:
        return []


def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) <= CALL_LOG_ROTATE:
            return
        os.replace(path, f"{path}.{dt.datetime.now():%Y%m%d-%H%M%S}")
    except OSError:
        return  # no call log yet, or another program has it open: next time
    cutoff = time.time() - LOG_DAYS * 86400
    for old in archives(path):
        try:
            if os.path.getmtime(old) < cutoff:
                os.remove(old)
        except OSError:
            pass


def write_call(path: str, entry: dict[str, Any]) -> None:
    """entry: agent, session, company, settings, command, args, outcome, ms, items, output_bytes, requests. Counts only,
    never values from the company file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _rotate(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(dumps({"time": _now(), "user": who_am_i(), **entry}) + "\n")


def read_log(path: str, since: float | None = None) -> list[dict[str, Any]]:
    """Every entry of a log, its older parts included, oldest first; since: skip parts last written before this
    time.time(). An unreadable line comes back as {"unreadable": the start of the line}."""
    out: list[dict[str, Any]] = []
    for part in [*archives(path), path]:
        try:
            if since is not None and part != path and os.path.getmtime(part) < since:
                continue
            with open(part, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            continue
        for line in lines:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"unreadable": line[:200]})
    return out


def read_audit(path: str, limit: int) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = [line for line in f.read().split("\n") if line]
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"unreadable": line[:200]})
    return out


def committed(path: str, company_file: str, source: str) -> dict[str, Any] | None:
    """The newest entry that saved, or may have saved, a change from this source to this company file."""
    return next((e for e in read_audit(path, 10 ** 9) if e.get("source") == source and e.get("outcome") in ("done", "unknown")
                 and text(e.get("companyFile", "")).lower() == company_file.lower()), None)
