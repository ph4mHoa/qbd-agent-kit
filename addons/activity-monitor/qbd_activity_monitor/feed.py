"""What qbd writes in its state folders, read as it changes: the call logs and write logs line by line as they grow
(across rotation), the drafts and their answers, the live files of running and waiting commands, the paused sessions
and the last facts read from QuickBooks. It only reads: it never opens QuickBooks, never takes qbd's lock and never
changes a file qbd wrote. qbd only adds to its logs and moves them whole, so a log that gets shorter or is replaced
by something else is noticed and reported (a detective control for the logs as evidence)."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from qbd_agent_kit.api import LIVE, PAUSED, alive, archives, file_digest, load_config, read_json, state_dir

from .words import iso_time

KEEP_DAYS = 90  # the most qbd keeps of its call log


@dataclass(slots=True)
class Rec:
    """One line of a call log or write log."""
    t: float     # when qbd wrote it (epoch)
    e: dict      # the line as written
    folder: str  # the state folder it came from
    demo: bool   # from the sample company's logs
    src: str     # the file it was read from, for Technical details
    line: int    # its line number there


@dataclass(slots=True)
class Skipped:
    """A line that is not JSON, such as half a line from a crash."""
    src: str
    line: int
    after: float | None  # the time of the line before it


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


class Log:
    """A JSONL log read as it grows. qbd moves a full call log to <name>.<date-time> and starts a new file, so a log
    whose file changed identity is first read to its end under its new name, then the new file from its start. A log
    that got shorter, or was replaced or removed, with no archive of it, was changed by something other than qbd: it is
    listed in cuts, also when that happened while the monitor was closed (mark: what it was then)."""

    def __init__(self, path: str, folder: str, demo: bool, since: float, mark: list | None = None):
        self.path, self.folder, self.demo, self.since = path, folder, demo, since
        self.ident: tuple[int, int] | None = None
        self.pos = 0
        self.lineno = 0
        self.last_t: float | None = None
        self.skipped: list[Skipped] = []
        self.started = False
        self.mark = mark
        self.cuts: list[tuple[float, str]] = []  # (when noticed, the log's file name)

    def poll(self) -> list[Rec]:
        out: list[Rec] = []
        try:
            st: os.stat_result | None = os.stat(self.path)
        except OSError:
            st = None
        if not self.started:
            self.started = True
            for old in archives(self.path):
                try:
                    if os.path.getmtime(old) >= self.since:
                        self._read(old, 0, 0, out)
                except OSError:
                    pass
            if st is not None:
                self.ident = _identity(st)
                self.pos, self.lineno = self._read(self.path, 0, 0, out)
            self._since_last_run(st)
            return out
        if self.ident is not None and (st is None or _identity(st) != self.ident or st.st_size < self.pos):
            moved = next((a for a in archives(self.path) if _same(a, self.ident)), None)
            if moved is not None:  # rotated: what was written before the move
                self._read(moved, self.pos, self.lineno, out)
            elif self.ident[1] != 0:
                self.cuts.append((time.time(), os.path.basename(self.path)))
            self.ident, self.pos, self.lineno = None, 0, 0
        if st is not None:
            if self.ident is None:
                self.ident = _identity(st)
            if st.st_size > self.pos:
                self.pos, self.lineno = self._read(self.path, self.pos, self.lineno, out)
        return out

    def _since_last_run(self, st: os.stat_result | None) -> None:
        mark = self.mark
        if not (isinstance(mark, list) and len(mark) == 4 and mark[1] and mark[3] >= time.time() - KEEP_DAYS * 86400):
            return
        ident, size = (mark[0], mark[1]), mark[2]
        if st is not None and _identity(st) == ident:
            shorter = st.st_size < size
        else:
            moved = next((a for a in archives(self.path) if _same(a, ident)), None)
            shorter = moved is None or os.path.getsize(moved) < size
        if shorter:
            self.cuts.append((time.time(), os.path.basename(self.path)))

    def marked(self) -> list | None:
        """What the log is now, for the next run's check."""
        return [*self.ident, self.pos, time.time()] if self.ident is not None and self.ident[1] else None

    def _read(self, path: str, pos: int, lineno: int, out: list[Rec]) -> tuple[int, int]:
        """Reads whole lines from pos on; a last line without its newline waits for the next poll."""
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
        except OSError:
            return pos, lineno
        end = data.rfind(b"\n")
        if end < 0:
            return pos, lineno
        for raw in data[: end + 1].split(b"\n")[:-1]:
            lineno += 1
            if not raw.strip():
                continue
            entry = _parse(raw)
            t = iso_time(entry.get("time")) if isinstance(entry, dict) else None
            if entry is None or t is None:
                self.skipped.append(Skipped(os.path.basename(path), lineno, self.last_t))
                continue
            self.last_t = t
            if t >= self.since:
                out.append(Rec(t, entry, self.folder, self.demo, path, lineno))
        return pos + end + 1, lineno


def _same(path: str, ident: tuple[int, int]) -> bool:
    try:
        return _identity(os.stat(path)) == ident and ident[1] != 0
    except OSError:
        return False


def _parse(raw: bytes) -> dict | None:
    """A line as JSON. A crash can leave half a line that the next one is appended to: the whole entry at its end is
    still read."""
    text = raw.decode("utf-8", errors="replace").strip()
    for start in (0, text.rfind('{"time":')):
        if start < 0:
            continue
        try:
            value = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class Files:
    """The JSON files of one folder, read again only when they change, with each file's SHA-256 when asked (the drafts:
    what a person approves is what they saw)."""

    def __init__(self, path: str, keep: Any, digests: bool = False):
        self.path, self.keep = path, keep  # keep(name) -> bool
        self.seen: dict[str, tuple[int, int]] = {}
        self.data: dict[str, Any] = {}
        self.digests: dict[str, str] | None = {} if digests else None

    def poll(self) -> bool:
        changed = False
        found: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(self.path) as it:
                for d in it:
                    if d.is_file() and self.keep(d.name):
                        try:
                            st = d.stat()
                        except OSError:
                            continue
                        found[d.name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        for name in list(self.data):
            if name not in found:
                del self.data[name]
                self.seen.pop(name, None)
                if self.digests is not None:
                    self.digests.pop(name, None)
                changed = True
        for name, sig in found.items():
            if self.seen.get(name) != sig:
                path = os.path.join(self.path, name)
                if self.digests is None:
                    value, digest = read_json(path), ""
                else:
                    value, digest = _json_and_digest(path)
                if value is None and name in self.data:
                    continue  # being rewritten: keep what was there
                self.seen[name] = sig
                self.data[name] = value
                if self.digests is not None:
                    self.digests[name] = digest
                changed = True
        return changed


def _json_and_digest(path: str) -> tuple[Any, str]:
    """A file's JSON and the SHA-256 of the same bytes."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return json.loads(raw), file_digest(raw)
    except (OSError, ValueError):
        return None, ""


@dataclass
class Folder:
    """One qbd state folder: the per-user one, or a shared one set with QBD_STATE_DIR."""
    path: str
    shared: bool
    call_logs: list[Log] = field(default_factory=list)
    write_logs: list[Log] = field(default_factory=list)
    drafts: Files | None = None
    live: Files | None = None
    paused: Files | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    facts_sig: dict[str, tuple[int, int]] = field(default_factory=dict)
    sample: bool = False  # the monitor's sample data (--sample): its live files name no real process


def folders(env: Mapping[str, str], extra: list[str] | None = None) -> list[tuple[str, bool]]:
    """The state folders to read: each Windows user's own, and the shared one when QBD_STATE_DIR names it."""
    own = state_dir({k: v for k, v in env.items() if k != "QBD_STATE_DIR"})
    out = [(own, False)]
    shared = (env.get("QBD_STATE_DIR") or "").strip()
    for path in ([os.path.abspath(shared)] if shared else []) + [os.path.abspath(p) for p in extra or []]:
        if os.path.normcase(path) not in {os.path.normcase(p) for p, _ in out}:
            out.append((path, True))
    return out


class Feed:
    """Everything qbd wrote, as it changes. poll() reads what is new and says what changed."""

    def __init__(self, env: Mapping[str, str], since: float, extra: list[str] | None = None, only: list[str] | None = None,
                 sample: bool = False, marks: dict[str, list] | None = None):
        self.env = dict(env)
        self.calls: list[Rec] = []
        self.changes: list[Rec] = []
        chosen = [(os.path.abspath(p), True) for p in only] if only else folders(env, extra)
        self.folders: list[Folder] = []
        marks = marks or {}
        for path, shared in chosen:
            # sample data only when asked for on the command line, and only in a folder the sample was written to
            f = Folder(path, shared, sample=sample and os.path.exists(os.path.join(path, "monitor-sample.txt")))
            local = {**self.env, "QBD_STATE_DIR": path}
            seen: set[str] = set()
            for demo in (False, True):
                cfg = load_config(local, demo=demo)
                for logs, p in ((f.call_logs, cfg.call_log or os.path.join(path, "calls-demo.jsonl" if demo else "calls.jsonl")),
                                (f.write_logs, cfg.audit_log)):
                    key = os.path.normcase(os.path.abspath(p))
                    if key not in seen:
                        seen.add(key)
                        logs.append(Log(p, path, demo, since, marks.get(key)))
            f.drafts = Files(os.path.join(path, "drafts"), lambda n: n.endswith(".json"), digests=True)
            f.live = Files(os.path.join(path, LIVE), lambda n: n.endswith(".json") or n.endswith(".stop"))
            f.paused = Files(os.path.join(path, PAUSED), lambda n: n.endswith(".json"))
            self.folders.append(f)

    def poll(self) -> set[str]:
        """Reads what changed: returns which of "calls", "changes", "drafts", "live", "paused", "facts" did."""
        changed: set[str] = set()
        for f in self.folders:
            for log in f.call_logs:
                new = log.poll()
                if new:
                    self.calls.extend(new)
                    changed.add("calls")
            for log in f.write_logs:
                new = log.poll()
                if new:
                    self.changes.extend(new)
                    changed.add("changes")
            for name, files in (("drafts", f.drafts), ("live", f.live), ("paused", f.paused)):
                if files is not None and files.poll():
                    changed.add(name)
            for name in ("quickbooks.json", "quickbooks-demo.json"):
                p = os.path.join(f.path, name)
                try:
                    st = os.stat(p)
                    sig = (st.st_mtime_ns, st.st_size)
                except OSError:
                    sig = None
                if f.facts_sig.get(name) != sig:
                    f.facts_sig[name] = sig  # type: ignore[assignment]
                    value = read_json(p) if sig else None
                    if value is not None or sig is None:
                        f.facts[name] = value
                        changed.add("facts")
        if "calls" in changed:
            self.calls.sort(key=lambda r: r.t)
        if "changes" in changed:
            self.changes.sort(key=lambda r: r.t)
        return changed

    def prune(self, since: float) -> None:
        self.calls = [r for r in self.calls if r.t >= since]
        self.changes = [r for r in self.changes if r.t >= since]

    # ------------------------------------------------------------ the current files, with their folder

    def drafts(self) -> list[tuple[Folder, str, dict]]:
        """(folder, file name, content) for every file in the drafts folders."""
        return [(f, n, d) for f in self.folders for n, d in sorted((f.drafts.data if f.drafts else {}).items()) if isinstance(d, dict)]

    def live(self) -> list[tuple[Folder, dict, bool]]:
        """(folder, live file, stop asked) for each command running or waiting now."""
        out = []
        for f in self.folders:
            data = f.live.data if f.live else {}
            for name, info in data.items():
                if name.endswith(".json") and isinstance(info, dict) and (f.sample or alive(info)):
                    out.append((f, info, f"{name[:-5]}.stop" in data))
        return out

    def paused(self) -> list[tuple[Folder, dict]]:
        return [(f, d) for f in self.folders for d in (f.paused.data if f.paused else {}).values() if isinstance(d, dict)]

    def facts(self) -> dict[str, dict]:
        """company file -> what qbd last read about it, newest read first wins."""
        out: dict[str, dict] = {}
        for f in self.folders:
            for name, data in f.facts.items():
                files = data.get("files") if isinstance(data, dict) and isinstance(data.get("files"), dict) else {}
                for path, facts in files.items():
                    if isinstance(facts, dict) and str(facts.get("read", "")) >= str(out.get(path, {}).get("read", "")):
                        out[path] = {**facts, "demo": name == "quickbooks-demo.json", "folder": f.path}
        return out

    def skipped(self) -> list[Skipped]:
        return [s for f in self.folders for log in f.call_logs + f.write_logs for s in log.skipped]

    def cuts(self) -> list[tuple[float, str, str]]:
        """(when noticed, log file name, folder) for each log changed by something other than qbd."""
        return [(t, name, f.path) for f in self.folders for log in f.call_logs + f.write_logs for t, name in log.cuts]

    def marks(self) -> dict[str, list]:
        """Each log as it is now, for the next run to tell whether it was cut while the monitor was closed."""
        return {os.path.normcase(os.path.abspath(log.path)): m for f in self.folders for log in f.call_logs + f.write_logs
                if (m := log.marked()) is not None}

    def call_log_on(self) -> bool:
        return bool(load_config(self.env).call_log)


def pc_env() -> dict[str, str]:
    """The settings an assistant started now would get: on Windows the QBD_ variables as the registry has them (a
    setx shows at once) over this process's environment; elsewhere this process's environment."""
    env = dict(os.environ)
    if sys.platform != "win32":
        return env
    try:
        import winreg
    except ImportError:
        return env
    stored: dict[str, str] = {}
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if name.upper().startswith("QBD_") and isinstance(value, str):
                        stored[name.upper()] = os.path.expandvars(value)
        except OSError:
            pass
    return {**env, **stored}
