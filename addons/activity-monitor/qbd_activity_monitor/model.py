"""What the Activity Monitor shows, worked out from what qbd wrote: assistants, sessions, problems, changes,
approvals, the line, the banners and the tray's state, for one company file or all of them, by the business rules
of the BA document. Plain Python without Qt, so it is tested like the rest of the kit. Every view is made fresh from
the feed; nothing here writes a file except the monitor's own small state (colours, problems seen)."""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable

from qbd_agent_kit.api import SAMPLE_COMPANY_FILE as SAMPLE_FILE
from qbd_agent_kit.api import STATUS_HINTS, TOKEN, Config, batch_tokens, fingerprint, load_config, who_am_i, write_json

from . import words as w
from .feed import KEEP_DAYS, Feed, Folder, Rec

SESSION_GAP = 30 * 60   # an assistant's requests closer than this, without a session ID, are one session
LINE_WAIT = 60          # how long a command waits for its turn
SLOW = 30               # from here, the slow note
NOT_REQUESTS = {"activity", "resume", "addons"}  # activity and addons read only qbd's own files; resume is the monitor's line
COLOURS = 6             # assistant colours, given in first-seen order
OVERVIEW_SESSIONS = 8
ALL = "*"               # the company-file pick that shows every file
CURRENCY = {"US": "USD", "CA": "CAD", "UK": "GBP", "GB": "GBP", "AU": "AUD"}  # by the QuickBooks edition's country
WRITES = {"draft", "import", "commit", "undo"}   # a later success of these is another change, not this one again
SETTLED = {"refused", "invalid_arguments", "invalid_request", "unconfirmed", "stopped", "paused"}  # nothing left to retry
BLOCKED, CLOSED = w.BLOCKED, w.CLOSED
AMOUNT_LINE = re.compile(r"(.+?):\s*(-?[\d,]+\.\d\d)")  # a preview line with an amount; others are fields (DueDate: ...)


def file_key(path: Any) -> str:
    return str(path or "").strip().replace("/", "\\").casefold()


def is_sample(path: Any) -> bool:
    return file_key(path) == file_key(SAMPLE_FILE)


# ---------------------------------------------------------------- the monitor's own state


class State:
    """monitor.json in the per-user state folder: the assistants in the order first seen (their colours), until when
    someone has seen the problems, each log as it was last read (to notice a log cut while the monitor was closed) and
    the logs found changed by something other than qbd."""

    def __init__(self, path: str | None):
        self.path = path
        data = None
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
        self.data: dict[str, Any] = data if isinstance(data, dict) else {}
        self.data.setdefault("assistants", [])
        self.data.setdefault("problems_seen", 0.0)
        self.data.setdefault("marks", {})
        self.data.setdefault("cuts", [])
        self.data.setdefault("portal", {})
        self.dirty = False

    def note_cuts(self, cuts: list[tuple[float, str, str]], now: float) -> bool:
        """Keeps the logs found cut for KEEP_DAYS; True when one is new."""
        known = {(c.get("file"), c.get("folder")) for c in self.data["cuts"] if isinstance(c, dict)}
        kept = [c for c in self.data["cuts"] if isinstance(c, dict) and float(c.get("t") or 0) >= now - KEEP_DAYS * 86400]
        new = [{"t": t, "file": name, "folder": folder} for t, name, folder in cuts if (name, folder) not in known]
        if new or len(kept) != len(self.data["cuts"]):
            self.data["cuts"] = kept + new
            self.dirty = True
        return bool(new)

    def save(self) -> None:
        if self.dirty and self.path:
            write_json(self.path, self.data)
        self.dirty = False


@dataclass(frozen=True)
class Who:
    key: str        # a:<name>, p:<program> or u:<Windows user>
    name: str       # "Claude (Claude Code)", "agy", or "Unknown assistant"
    initials: str   # C, n8, ?
    colour: int     # 0-5 in first-seen order; -1 for an unknown caller (grey)
    user: str       # the Windows user, shown next to an unknown assistant
    short: str      # "Claude", for sentences

    @property
    def unknown(self) -> bool:
        return self.colour < 0


class Assistants:
    """Each assistant keeps one name, initials and colour everywhere (GL-3, BR 13)."""

    def __init__(self, state: State):
        self.state = state
        self.order: list[str] = state.data["assistants"]
        self.cache: dict[tuple, Who] = {}

    def of(self, e: dict) -> Who:
        agent, program, user = str(e.get("agent") or "").strip(), w.program_name(e.get("program")), w.windows_user(e.get("user"))
        found = self.cache.get((agent, program, user))
        if found is not None:
            return found
        if agent or program:
            name = agent or program
            key = ("a:" if agent else "p:") + name.casefold()
            if key not in self.order:
                self.order.append(key)
                self.state.dirty = True
            who = Who(key, name, w.initials(name), self.order.index(key) % COLOURS, user, (re.split(r"\s*\(", name)[0] or name).split()[0])
        else:
            who = Who("u:" + user.casefold(), "Unknown assistant", "?", -1, user, "the assistant")
        self.cache[(agent, program, user)] = who
        return who


# ---------------------------------------------------------------- what the views hold


@dataclass
class FileChoice:
    key: str
    name: str
    detail: str      # "21 requests today"
    sample: bool


@dataclass
class Banner:
    kind: str        # closed, blocked, timeout, busy, paused, call_log_off, log_cut
    tone: str        # bad, warn, info
    title: str
    text: str
    more: str = ""
    action: str = ""  # live, safety, resume
    data: Any = None


@dataclass
class LiveCmd:
    """A command that runs now or waits for its turn, from its live file."""
    folder: Folder
    pid: int
    info: dict
    who: Who
    running: bool
    title: str
    args: str
    session_title: str
    started: float
    since: float            # when it got its turn (running) or started waiting
    file: str
    company: str
    step: str = ""          # Transactions
    doing: str = ""         # QuickBooks is building the A/R aging report…
    page: int | None = None
    pages: int | None = None
    read: int | None = None
    total: int | None = None
    percent: int | None = None
    cap: int | None = None
    capped: bool = False
    report: bool = False
    step_since: float = 0.0
    done: list[tuple[str, str]] = field(default_factory=list)
    stop_asked: bool = False
    stoppable: bool = True
    stop_note: str = ""

    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.since)


@dataclass
class SessionRow:
    sid: str
    who: Who
    session: str             # the assistant's session ID, or ""
    asked: str               # what it was asked, or the titles of its first three requests
    titled: bool             # asked is the assistant's own title
    start: float
    end: float | None        # None while it runs or waits
    calls: list[Rec]
    changes: list[Rec]
    drafts: int
    live: list[LiveCmd]
    requests: int
    records: int
    saved: int
    problems: int
    check: int               # changes QuickBooks did not confirm
    badge: tuple[str, str, str]
    files: list[str]

    def time_text(self) -> str:
        return f"{w.clock(self.start)}–{'now' if self.end is None else w.clock(self.end)}"

    def counts_text(self) -> str:
        if not self.calls and self.live:
            return "waiting for its turn" if not any(c.running for c in self.live) else "running its first request"
        bits = [w.plural(self.requests, "request"), w.plural(self.records, "record")]
        if self.saved:
            bits.append(f"{w.plural(self.saved, 'change')} saved")
        if self.problems:
            bits.append(w.plural(self.problems, "problem"))
        return " · ".join(bits)


@dataclass
class RequestRow:
    rec: Rec
    who: Who
    title: str
    args: str
    badge: tuple[str, str, str]
    ms: int
    records: int
    asked: list[tuple[str, str]]
    defaults_only: bool
    steps: list[tuple[str, str]]
    problem: dict | None
    technical: str


@dataclass
class SessionDetail:
    row: SessionRow
    span: str
    company: str
    totals: list[tuple[str, str]]
    settings_note: tuple[str, str]
    requests: list[RequestRow]
    longest: int
    changes: list["ChangeRow"]


@dataclass
class ProblemRow:
    pid: str
    first: Rec
    last: Rec
    count: int
    who: Who
    title: str
    args: str
    badge: tuple[str, str, str]
    text: str
    steps: list[str]
    status: tuple[str, str]   # (words, tone) for the right-hand column
    solved: str               # "Solved: the same request worked at 09:51."
    open: bool
    new: bool
    technical: list[tuple[str, str]]


@dataclass
class ChangeRow:
    rec: Rec
    who: Who
    title: str
    about: str
    amount: str
    badge: tuple[str, str, str]
    reason: str
    saved: str
    diff: list[tuple[str, str, str]]
    undo: bool
    undo_text: str
    explain: str
    todo: str


@dataclass
class Approval:
    folder: Folder
    token: str
    d: dict
    who: Who
    sample: bool
    title: str
    sentence: str
    lines: list[tuple[str, str]]
    total: str
    currency: str
    warnings: list[str]
    checks: list[str]
    reason: str
    session_title: str
    created: float
    expires: float
    expired: bool
    can_approve: bool
    why_not: str
    note: str                 # drafted with other settings than this PC's
    seen: str = ""            # the fingerprint of the files shown; qbd refuses the approval if they changed since

    @property
    def brief(self) -> str:
        """4 customer payments · from Claude (Claude Code)"""
        noun = "customer payment" if self.d.get("entity") == "deposit" else "record" if self.d.get("action") == "batch" else "line"
        n = sum(1 for _label, amount in self.lines if amount) or len(self.lines)
        return " · ".join(x for x in [w.plural(n, noun) if self.lines else "", f"from {self.who.name}"] if x)

    @property
    def flag(self) -> str:
        """The first warning's name, such as Possible duplicate."""
        return self.warnings[0].split(":")[0] if self.warnings else ""

    @property
    def columns(self) -> tuple[str, ...]:
        """The heads of the lines' table; three columns when each line is an account and a description."""
        if self.d.get("entity") == "deposit":
            return "Payment from", "Amount"
        if self.d.get("action") == "batch":
            return "Record", ""
        if self.lines and all(", " in label for label, amount in self.lines if amount):
            return "Account", "Description", "Amount"
        return "Line", "Amount"


@dataclass
class Answered:
    t: float
    title: str
    who: Who
    badge: tuple[str, str, str]
    how: str
    token: str
    undo: bool


@dataclass
class Overview:
    date_line: str
    tiles: list[tuple[str, str, str, str]]   # (label, value, note, tone)
    running: LiveCmd | None
    waiting: list[LiveCmd]
    needs: list[dict]
    sessions: list[SessionRow]
    sessions_today: int
    banners: list[Banner]
    quiet: str
    first_run: bool


@dataclass
class Tray:
    state: str                 # waiting, problem, working, idle
    tooltip: str
    badge: tuple[str, str, str] | None
    running: LiveCmd | None
    lines: list[tuple[str, str, str, str, str]]  # (icon, tone, title, detail, page)
    footer: str


@dataclass
class Portal:
    """The portal's one state, the most pressing (the Portal board), in words a notification may use: who, the
    request's title, counts and times, never an amount or a name from the books."""
    state: str                 # running, stopping, stopped, waiting, blocked, closed, timeout, line, done, idle
    since: float = 0.0         # where its time counts from: running since, stopped, the timed-out request began, finished
    who: Who | None = None
    cmd: LiveCmd | None = None
    title: str = ""
    detail: str = ""
    text: str = ""
    badge: tuple[str, str, str] | None = None
    resume: tuple[Folder, dict] | None = None
    expires: float = 0.0       # waiting: when the first change waiting for an answer expires
    line: int = 0              # commands waiting for their turn
    need: int = 0              # changes waiting for an answer
    page: str = "live"         # where Expand opens the window


# ---------------------------------------------------------------- the model


class Model:
    def __init__(self, feed: Feed, state: State, settings: Callable[[], dict[str, str]] | None = None,
                 quickbooks: Callable[[], bool | None] | None = None, clock: Callable[[], float] = time.time,
                 tools: dict | None = None):
        self.feed = feed
        self.state = state
        self.assistants = Assistants(state)
        self.settings_env = settings or (lambda: dict(feed.env))
        self.quickbooks = quickbooks or (lambda: None)
        self.clock = clock
        self.tools = tools
        self.qb_running: bool | None = None
        self._groups: dict[str, list[SessionRow]] = {}

    # ------------------------------------------------------------ reading

    def poll(self) -> set[str]:
        changed = self.feed.poll()
        if self.state.note_cuts(self.feed.cuts(), self.now()):
            changed.add("cuts")
            self.state.save()
        if changed & {"calls", "changes", "drafts", "live"}:
            self._groups.clear()
        running = self.quickbooks()
        if running != self.qb_running:
            self.qb_running = running
            changed.add("quickbooks")
        return changed

    def now(self) -> float:
        return self.clock()

    def midnight(self) -> float:
        d = w.local(self.now())
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def pc(self) -> Config:
        """This PC's settings, as an assistant started now would get them."""
        return load_config(self.settings_env())

    # ------------------------------------------------------------ company files (GL-4, BR 16)

    def _call_file(self, r: Rec) -> str:
        return file_key(r.e.get("company_file"))

    def _change_file(self, r: Rec) -> str:
        return file_key(r.e.get("companyFile"))

    @staticmethod
    def _in(key: str, pick: str) -> bool:
        return pick == ALL or not key or key == pick

    def names(self) -> dict[str, tuple[str, bool]]:
        """file key -> (company name, sample company)."""
        found: dict[str, tuple[str, str, bool]] = {}  # key -> (path, name, sample)
        for path, facts in self.feed.facts().items():
            found[file_key(path)] = (path, str(facts.get("company") or ""), bool(facts.get("demo")) or is_sample(path))
        for r in self.feed.calls:
            path = r.e.get("company_file")
            if path:
                old = found.get(file_key(path))
                found[file_key(path)] = (path, str(r.e.get("company") or (old[1] if old else "")), r.demo or is_sample(path))
        for r in self.feed.changes:
            path = r.e.get("companyFile")
            if path and file_key(path) not in found:
                found[file_key(path)] = (path, "", r.demo or is_sample(path))
        for _, name, d in self.feed.drafts():
            path = d.get("company_file")
            if path and file_key(path) not in found and not name.endswith(".answer.json"):
                found[file_key(path)] = (path, "", is_sample(path))
        out: dict[str, tuple[str, bool]] = {}
        stems = {key: re.sub(r"\.qbw$", "", re.split(r"[\\/]", path)[-1], flags=re.I) for key, (path, _, _) in found.items()}
        for key, (path, name, sample) in found.items():
            same = [stems[k] for k, v in found.items() if v[1] == name] if name else []
            extra = stems[key][len(_common_start(same)):].strip(" -_") if len(same) > 1 else ""
            out[key] = (f"{name} – {extra}" if name and extra else name or stems[key], sample)
        return out

    def files(self) -> list[FileChoice]:
        names = self.names()
        today = self.midnight()
        counts: dict[str, int] = {}
        newest: dict[str, float] = {}
        for r in self.feed.calls:
            k = self._call_file(r)
            if k:
                newest[k] = r.t
                if r.t >= today and r.e.get("command") not in NOT_REQUESTS:
                    counts[k] = counts.get(k, 0) + 1
        order = sorted(names, key=lambda k: -newest.get(k, 0))
        out = []
        for k in order:
            n = counts.get(k, 0)
            when = f"{w.plural(n, 'request')} today" if n else (f"last request {w.ago(newest[k], self.now())}" if k in newest else "no requests yet")
            out.append(FileChoice(k, names[k][0], when, names[k][1]))
        return out

    def default_file(self) -> str:
        """The file of the newest request (BR 16); every file when there is none."""
        for r in reversed(self.feed.calls):
            k = self._call_file(r)
            if k:
                return k
        return ALL

    def company_name(self, pick: str) -> str:
        if pick == ALL:
            return ""
        return self.names().get(pick, ("", False))[0]

    def currency(self, path: Any) -> str:
        facts = self.feed.facts()
        f = next((v for p, v in facts.items() if file_key(p) == file_key(path)), None)
        return CURRENCY.get(str((f or {}).get("country") or "").upper(), "")

    # ------------------------------------------------------------ calls and changes in a period

    def calls(self, pick: str, since: float = 0.0) -> list[Rec]:
        """Requests in the period: every command except qbd activity (BR 15) and the monitor's own resume lines."""
        return [r for r in self.feed.calls if r.t >= since and r.e.get("command") not in NOT_REQUESTS and self._in(self._call_file(r), pick)]

    def changes_in(self, pick: str, since: float = 0.0) -> list[Rec]:
        return [r for r in self.feed.changes if r.t >= since and self._in(self._change_file(r), pick)]

    # ------------------------------------------------------------ live: running and waiting (G2, G4)

    def live(self, pick: str = ALL) -> tuple[LiveCmd | None, list[LiveCmd]]:
        """The command talking to QuickBooks now, and the ones waiting for their turn, first in line first."""
        names = self.names()
        running: list[LiveCmd] = []
        waiting: list[LiveCmd] = []
        for folder, info, stop in self.feed.live():
            state = info.get("state")
            if state not in ("running", "waiting", "starting"):
                continue
            key = file_key(info.get("company_file"))
            if not self._in(key, pick):
                continue
            cmd = self._live_cmd(folder, info, stop, names)
            (running if cmd.running else waiting).append(cmd)
        waiting.sort(key=lambda c: (c.info.get("queued_ns") or 0, c.pid))
        running.sort(key=lambda c: c.since)
        return (running[-1] if running else None), waiting

    def _live_cmd(self, folder: Folder, info: dict, stop: bool, names: dict) -> LiveCmd:
        who = self.assistants.of(info)
        started = w.iso_time(info.get("started")) or self.now()
        is_running = info.get("state") == "running"
        since = w.iso_time(info.get("running_since") if is_running else info.get("queued")) or started
        key = file_key(info.get("company_file"))
        company = str(info.get("company") or names.get(key, ("", False))[0] or "")
        cmd = LiveCmd(folder, int(info.get("pid") or 0), info, who, is_running, str(info.get("title") or info.get("command") or "qbd"),
                      w.args_line(info), str(info.get("session_title") or ""), started, since, key, company,
                      stop_asked=stop, stoppable=info.get("stoppable", True) is not False)
        step = info.get("step") if isinstance(info.get("step"), dict) else None
        done = [d for d in info.get("done") or [] if isinstance(d, dict)]
        if step:
            request, report = step.get("request"), step.get("report")
            cmd.step = w.request_label(request, report)
            cmd.report = bool(report)
            cmd.step_since = w.iso_time(step.get("since")) or since
            if "page" in step:
                read, left, size, cap = (step.get(k) for k in ("read", "left", "page_size", "cap"))
                cmd.page = int(step["page"])
                if isinstance(read, int) and isinstance(left, int):
                    total = read + left
                    cmd.capped = isinstance(cap, int) and total > cap
                    total = min(total, cap) if isinstance(cap, int) else total
                    cmd.read, cmd.total, cmd.cap = read, total, cap if cmd.capped else None
                    cmd.percent = min(99, round(read * 100 / total)) if total else 100  # 100 only once it is read
                    if isinstance(size, int) and size > 0:
                        cmd.pages = max(cmd.page, -(-total // size))
                if done and done[-1].get("request") == request:
                    done = done[:-1]  # the earlier pages of this same step
            cmd.doing = w.doing(request, report, "page" in step and cmd.percent is None)
        for d in done:
            pages = d.get("pages", 1)
            bits = [f"{pages} pages" if pages > 1 else "", w.plural(d["read"], "record") if isinstance(d.get("read"), int) else "",
                    w.secs(d.get("ms"))]
            cmd.done.append((w.request_label(d.get("request"), d.get("report")), "done · " + " · ".join(b for b in bits if b)))
        name = who.short if not who.unknown else "the assistant"
        if not cmd.stoppable:
            cmd.stop_note = "A single change is sent whole, so there is no step to stop after."
        elif str(info.get("command")) in ("commit", "import"):
            cmd.stop_note = (f"The import stops before its next record; the records already saved stay saved, and {name}'s next "
                             "command waits until you click Resume.")
        elif cmd.report:
            cmd.stop_note = (f"QuickBooks finishes the report either way. The command ends when the report is back, and {name}'s "
                             "next command waits until you click Resume.")
        else:
            cmd.stop_note = (f"QuickBooks can't cancel a request halfway. The command ends when this "
                             f"{'page' if cmd.page else 'request'} is read, what it read is set aside, and {name}'s next command "
                             "waits until you click Resume.")
        return cmd

    def paused(self, pick: str = ALL) -> list[tuple[Folder, dict, Who]]:
        return [(f, d, self.assistants.of(d)) for f, d in self.feed.paused()]

    # ------------------------------------------------------------ sessions (BR 12)

    def sessions(self, pick: str, since: float = 0.0) -> list[SessionRow]:
        """Every session with a request, change, draft or live command in the period, newest first."""
        cache_key = f"{pick}|{since}"
        if cache_key in self._groups:
            return self._groups[cache_key]
        now = self.now()
        groups: dict[str, dict] = {}
        open_by: dict[str, dict] = {}  # assistant key -> its newest session without an ID

        def place(who: Who, session: str, start: float, end: float, title: str) -> dict:
            if session:
                sid = f"{who.key}|s:{session}"
            else:
                g = open_by.get(who.key)
                sid = g["sid"] if g is not None and start - g["end"] <= SESSION_GAP else f"{who.key}|t:{start:.3f}"
            g = groups.get(sid)
            if g is None:
                g = groups[sid] = {"sid": sid, "who": who, "session": session, "start": start, "end": end, "calls": [],
                                   "changes": [], "drafts": 0, "live": [], "title": "", "files": set()}
            g["start"], g["end"] = min(g["start"], start), max(g["end"], end)
            if title:
                g["title"] = title
            if not session:
                open_by[who.key] = g
            return g

        for r in self.calls(pick, since):
            who = self.assistants.of(r.e)
            ms = r.e.get("ms") if isinstance(r.e.get("ms"), (int, float)) else 0
            g = place(who, str(r.e.get("session") or ""), r.t - ms / 1000, r.t, str(r.e.get("session_title") or ""))
            g["calls"].append(r)
            if r.e.get("company_file"):
                g["files"].add(self._call_file(r))
        for r in self.changes_in(pick, since):
            if r.e.get("outcome") == "rejected":
                continue
            who = self.assistants.of(r.e)
            g = place(who, str(r.e.get("session") or ""), r.t, r.t, "")
            g["changes"].append(r)
        for _, name, d in self.feed.drafts():
            if not _is_draft(name) or not self._in(file_key(d.get("company_file")), pick) or _num(d.get("expires")) < now:
                continue
            created = _num(d.get("created")) or now
            if created < since:
                continue
            g = place(self.assistants.of(d), str(d.get("session") or ""), created, created, str(d.get("session_title") or ""))
            g["drafts"] += 1
        running, waiting = self.live(pick)
        for cmd in ([running] if running else []) + waiting:
            g = place(cmd.who, str(cmd.info.get("session") or ""), cmd.started, now, cmd.session_title)
            g["live"].append(cmd)
        rows = [self._row(g) for g in groups.values()]
        rows.sort(key=lambda s: (any(c.running for c in s.live), bool(s.live), s.end or s.start, s.start), reverse=True)
        self._groups[cache_key] = rows
        return rows

    def _row(self, g: dict) -> SessionRow:
        calls: list[Rec] = sorted(g["calls"], key=lambda r: r.t)
        changes: list[Rec] = g["changes"]
        if g["title"]:
            asked, titled = w.mask(g["title"]), True
        else:
            titles = list(dict.fromkeys(str(r.e.get("title") or r.e.get("command") or "") for r in calls))
            if not titles:
                titles = [c.title for c in g["live"]]
            asked, titled = ", ".join(titles[:3]) + (", …" if len(titles) > 3 else ""), False
        requests = len(calls)
        records = sum(r.e.get("items", 0) for r in calls if isinstance(r.e.get("items"), int))
        saved = sum(1 for r in changes if r.e.get("outcome") == "done")
        failed = [r for r in calls if r.e.get("outcome") != "ok"]
        not_saved = _not_saved(changes, failed)
        check = sum(1 for r in changes if r.e.get("outcome") == "unknown")
        problems = len(failed) + len(not_saved)
        live: list[LiveCmd] = g["live"]
        if any(c.running for c in live):
            badge = ("Running", "info", "running")
        elif live:
            badge = ("In line", "warn", "clock")
        elif g["drafts"]:
            badge = (f"{g['drafts']} need you", "warn", "approval")
        elif check or any(r.e.get("outcome") == "unconfirmed" for r in failed):
            badge = ("Check it", "warn", "info")
        elif problems:
            badge = (w.plural(problems, "problem"), "bad", "x")
        else:
            badge = ("Done", "ok", "check_circle")
        return SessionRow(g["sid"], g["who"], g["session"], asked, titled, g["start"], None if live else g["end"], calls, changes,
                          g["drafts"], live, requests, records, saved, problems, check, badge, sorted(g["files"]))

    def session(self, sid: str, pick: str = ALL) -> SessionDetail | None:
        row = next((s for s in self.sessions(pick, self.now() - KEEP_DAYS * 86400) if s.sid == sid), None)
        if row is None:
            return None
        now = self.now()
        names = self.names()
        companies = [names.get(k, ("", False))[0] for k in row.files] or ["file unknown"]
        user = next((str(r.e.get("user")) for r in row.calls if r.e.get("user")), "")
        name, _, pc = user.partition("@")
        end = now if row.end is None else row.end
        bits = [f"{w.short_day(row.start, now)} {row.time_text()}", w.secs((end - row.start) * 1000), ", ".join(companies)]
        if name:
            bits.append(f"Windows user {name}" + (f" on {pc}" if pc else ""))
        qb_ms = sum(q.get("ms", 0) for r in row.calls for q in r.e.get("requests") or [] if isinstance(q, dict) and isinstance(q.get("ms"), (int, float)))
        totals = [("Requests", w.num(row.requests)), ("Records read", w.num(row.records)), ("Changes saved", w.num(row.saved)),
                  ("Problems", w.num(row.problems)), ("Time in QuickBooks", w.secs(qb_ms))]
        requests = [self._request(r, row.who) for r in row.calls]
        longest = max([q.ms for q in requests] + [1])
        changes = [self.change_row(r) for r in row.changes]
        return SessionDetail(row, " · ".join(bits), ", ".join(companies), totals, self._settings_note(row), requests, longest, changes)

    def _settings_note(self, row: SessionRow) -> tuple[str, str]:
        """How the session was allowed to work, and whether that differs from this PC's settings (SA-3)."""
        used = [r.e.get("settings") for r in row.calls if isinstance(r.e.get("settings"), dict)]
        if not used:
            return ("", "")
        s = used[-1]
        differs = self._differs(s, self.pc())
        if not s.get("writes"):
            text = "Ran read-only: changes were not allowed for this session."
        else:
            bits = [f"up to {w.money(s['max_amount'])}" if isinstance(s.get("max_amount"), (int, float)) else "",
                    "deletes allowed" if s.get("deletes") else "no deletes"]
            text = f"Changes were allowed, each approved first ({', '.join(b for b in bits if b)})."
        if differs:
            return (f"{text} This PC's settings differ: {differs}. An assistant can set these in its own command line.", "warn")
        return (text, "")

    def _differs(self, s: dict, pc: Config) -> str:
        """What a session's settings allowed beyond this PC's, in words; "" when nothing."""
        out = []
        if s.get("writes") and not pc.allow_writes:
            out.append("changes allowed on a read-only PC")
        elif s.get("writes"):
            if s.get("deletes") and not pc.allow_delete:
                out.append("deletes allowed")
            if isinstance(s.get("max_amount"), (int, float)) and s["max_amount"] > pc.max_amount:
                out.append(f"a limit of {w.money(s['max_amount'])}")
            more = [x for x in s.get("entities") or [] if x not in pc.write_entities]
            if more:
                out.append("more record types (" + ", ".join(x.replace("_", " ") for x in more) + ")")
        if s.get("mask") is False and pc.mask_personal_data:
            out.append("personal data not masked")
        return ", ".join(out)

    def _request(self, r: Rec, who: Who) -> RequestRow:
        e = r.e
        steps = [(s["label"], w.step_text(s)) for s in w.fold_steps(e.get("requests"))]
        asked = w.asked_for(e.get("command"), e.get("args"), self.tools)
        given = e.get("args") if isinstance(e.get("args"), dict) else {}
        badge = w.outcome_badge(e.get("outcome"), e.get("error"))
        if e.get("outcome") == "ok" and e.get("command") in ("draft", "import", "undo"):
            badge = ("Previewed, not saved", "warn", "approval")
        problem = None if e.get("outcome") == "ok" else w.problem(e, who.name, str(e.get("company") or ""))
        return RequestRow(r, who, str(e.get("title") or f"qbd {e.get('command', '?')}"), w.args_line(e), badge,
                          int(e.get("ms") or 0), int(e.get("items") or 0), asked, not given, steps, problem,
                          json.dumps(e, indent=2, ensure_ascii=False))

    def session_start(self, cmd: LiveCmd) -> float:
        """When the session of a running or waiting command started."""
        sid = self.session_at(cmd.who, str(cmd.info.get("session") or ""), cmd.started)
        row = next((s for s in self.sessions(ALL, cmd.started - 86400) if s.sid == sid), None)
        return row.start if row is not None else cmd.started

    def session_at(self, who: Who, session: str, t: float) -> str | None:
        """The session a draft or a command belongs to: by the assistant's session ID, else the one around its time."""
        mine = [s for s in self.sessions(ALL, t - 86400) if s.who.key == who.key]
        found = next((s for s in mine if session and s.session == session), None)
        found = found or next((s for s in mine if s.start - 60 <= t <= (s.end or self.now()) + SESSION_GAP), None)
        return found.sid if found else None

    # ------------------------------------------------------------ problems (PR-1 to PR-4)

    def problems(self, pick: str, since: float) -> list[ProblemRow]:
        """Failed, blocked and refused requests, newest first; one assistant's repeats of one failure fold into one
        row per day (PR-3)."""
        calls = self.calls(pick, since)
        seen = float(self.state.data.get("problems_seen") or 0)
        rows: dict[tuple, ProblemRow] = {}
        later_ok: dict[tuple, float] = {}
        for r in reversed(calls):  # newest first, so "worked later" is known when an older failure comes
            who = self.assistants.of(r.e)
            same = (who.key, r.e.get("command"), json.dumps(r.e.get("args"), sort_keys=True))
            if r.e.get("outcome") == "ok":
                if r.e.get("command") not in WRITES:
                    later_ok[same] = r.t
                continue
            code = str(r.e.get("outcome"))
            kind = w.refusal(r.e.get("error")) if code == "refused" else ""
            key = (who.key, r.e.get("command"), code, kind, w.local(r.t).date())
            row = rows.get(key)
            if row is not None:
                row.first, row.count = r, row.count + 1
                row.new = row.new or r.t > seen
                continue
            rows[key] = self._problem(r, who, later_ok.get(same), seen, pick)
        out = sorted(rows.values(), key=lambda p: p.last.t, reverse=True)
        for p in out:
            if p.count > 1:
                p.args = (p.args + " · " if p.args else "") + f"{p.count} times since {w.clock(p.first.t)}"
        return out

    def _problem(self, r: Rec, who: Who, worked: float | None, seen: float, pick: str) -> ProblemRow:
        e = r.e
        code = str(e.get("outcome"))
        company = str(e.get("company") or self.names().get(self._call_file(r), ("", False))[0] or self.company_name(pick))
        p = w.problem(e, who.name, company)
        badge = w.outcome_badge(code, e.get("error"))
        if e.get("command") == "commit" and badge[0] == "QuickBooks refused":  # the same word as its row in Changes
            badge = w.change_badge("failed")
        if worked is not None:
            now = self.now()
            same_day = w.short_day(worked, now) == w.short_day(r.t, now)
            day = w.short_day(worked, now)
            on = f"at {w.clock(worked)}" if same_day else f"{day.lower() if day in ('Today', 'Yesterday') else day} {w.clock(worked)}"
            status, solved = (f"Worked {on}", "ok"), f"Solved: the same request worked {on}."
        elif code == "refused":
            status, solved = ("nothing was saved", "muted"), ""
        elif code == "unconfirmed":
            status, solved = ("check before retrying", "muted"), ""
        elif code in ("stopped", "paused"):
            status, solved = ("nothing was saved", "muted"), ""
        elif code in ("invalid_arguments", "invalid_request"):
            status, solved = ("nothing was read", "muted"), ""
        elif e.get("command") == "commit":
            status, solved = ("see Changes", "muted"), ""
        else:
            status, solved = ("still open", "warn"), ""
        technical = [("Error", code), ("qbd's message", w.cut(e.get("error"), 400))]
        if e.get("hint"):
            technical.append(("qbd's hint", str(e["hint"])))
        args = e.get("args")
        technical.append(("Command", f"{e.get('command', '?')} {w.mask(' '.join(f'{k} {v}' for k, v in args.items()) if isinstance(args, dict) else ' '.join(map(str, args or [])))}".strip()))
        run_by = who.name if not who.unknown else f"Unknown assistant ({who.user})"
        user = str(e.get("user") or "")
        technical.append(("Run by", f"{run_by}, as Windows user {user.replace('@', ' on ')}, after {w.secs(e.get('ms'))}" if user else run_by))
        technical.append(("Company file", str(e.get("company_file")) if e.get("company_file") else
                          "unknown: QuickBooks never opened one, so this shows under every file"))
        technical.append(("Logged in", f"{os.path.basename(r.src)}, line {w.num(r.line)}"))
        return ProblemRow(f"{os.path.basename(r.src)}:{r.line}", r, r, 1, who, str(e.get("title") or e.get("command") or "qbd"),
                          w.args_line(e) if w.args_line(e) != "defaults" else "", badge, p["text"], p["steps"], status, solved,
                          worked is None and code not in SETTLED and e.get("command") != "commit", r.t > seen, technical)

    def mark_problems_seen(self) -> None:
        self.state.data["problems_seen"] = self.now()
        self.state.dirty = True

    def new_problems(self) -> list[ProblemRow]:
        return [p for p in self.problems(ALL, self.now() - 7 * 86400) if p.new]

    # ------------------------------------------------------------ QuickBooks' state: closed, blocked, timed out

    def _unsettled(self, pick: str, codes: set[str]) -> Rec | None:
        """The newest failure with one of these codes that no later QuickBooks answer cleared."""
        for r in reversed(self.calls(pick, self.now() - 86400)):
            if r.e.get("outcome") == "ok" and r.e.get("requests"):
                return None
            if str(r.e.get("outcome")) in codes:
                return r
        return None

    def banners(self, pick: str) -> list[Banner]:
        now = self.now()
        out: list[Banner] = []
        sample = pick != ALL and self.names().get(pick, ("", False))[1]
        company = self.company_name(pick) or "the company file"
        closed = self._unsettled(pick, CLOSED)
        if not sample and (self.qb_running is False or closed is not None):
            out.append(Banner("closed", "bad", "QuickBooks is closed",
                              f"Assistants can't read the books until someone opens {company}."))
        blocked = self._unsettled(pick, BLOCKED)
        if blocked is not None:
            out.append(Banner("blocked", "warn", "Waiting for you",
                              "A QuickBooks window is open and waiting for someone. Close it, then retry.",
                              f"It may be hidden behind other windows. {blocked.e.get('title') or 'A request'} could not run at "
                              f"{w.clock(blocked.t)}.", "problems"))
        timeout = self._unsettled(pick, {"timeout"})
        if timeout is not None:
            who = self.assistants.of(timeout.e)
            gave_up = w.span(float(timeout.e.get("ms") or 0) / 1000)
            out.append(Banner("timeout", "warn", "Still running inside QuickBooks", f"for {w.span(now - timeout.t)}",
                              f"The assistant gave up after {gave_up}; QuickBooks may be slow until it finishes. "
                              f"{timeout.e.get('title') or 'A request'}, {who.name}.", "live"))
        running, waiting = self.live(pick)
        if running is not None and waiting and waiting[0].elapsed(now) >= SLOW:  # a short wait is the line working
            first = waiting[0]
            out.append(Banner("busy", "warn", "Busy",
                              f"QuickBooks is busy with {running.who.short}'s request. {first.who.short} has waited "
                              f"{w.span(first.elapsed(now))}.",
                              f"After {LINE_WAIT} s {first.who.short} gives up with \"busy\", which is safe to retry, and it "
                              "shows under Problems.", "live"))
        for folder, info, who in self.paused(pick):
            out.append(Banner("paused", "info", f"{who.name} is paused",
                              f"Its next command waits until you resume. Paused by {w.windows_user(info.get('by'))} "
                              f"{w.ago(w.iso_time(info.get('at')) or now, now)}.", "", "resume", (folder, info)))
        if not self.feed.call_log_on():
            out.append(Banner("call_log_off", "info", "The call log is off on this PC, so only changes are shown.",
                              "Requests, Records read, Sessions and Live stay empty. Changes and Approvals still work.", "", "safety"))
        for cut in self.cuts(now - 7 * 86400):
            out.append(Banner("log_cut", "bad", "A log was changed outside qbd",
                              f"{cut[1]} got shorter or was replaced (noticed {w.when(cut[0], now).replace('Yesterday', 'yesterday')}).",
                              "qbd only adds to its logs, so entries may be missing. Ask who has access to the folder; a copy kept "
                              "elsewhere has them.", "safety"))
        return out

    def cuts(self, since: float) -> list[tuple[float, str, str]]:
        """(noticed, file, folder) of the logs changed by something other than qbd since a time, newest first."""
        out = [(float(c.get("t") or 0), str(c.get("file")), str(c.get("folder"))) for c in self.state.data["cuts"] if isinstance(c, dict)]
        return sorted((c for c in out if c[0] >= since), reverse=True)

    # ------------------------------------------------------------ approvals (AP-1 to AP-6, G6, G7)

    def approvals(self, pick: str) -> tuple[list[Approval], list[Approval], list[Answered]]:
        """Drafts waiting for an answer, drafts that expired unanswered (still on file), and answers of the last 7 days."""
        now = self.now()
        pc = self.pc()
        waiting, expired, answered = [], [], []
        drafts = self.feed.drafts()
        records = {(f.path, t) for f, n, d in drafts if _is_draft(n) and d.get("action") == "batch" for t in batch_tokens(d.get("request"))}
        for folder, name, d in drafts:
            token = _token_of(name)
            if token is None or str(d.get("token")) != token or (folder.path, token) in records:
                continue  # not a draft qbd filed, or a record of an import (the import is the one shown)
            if not self._in(file_key(d.get("company_file")), pick):
                continue
            if _is_draft(name):
                a = self._approval(folder, d, pc, now)
                (expired if a.expired else waiting).append(a)
            elif name.endswith(".expired.json"):
                who = self.assistants.of(d)
                t = _num(d.get("expires"))
                if t >= now - 7 * 86400:
                    answered.append(Answered(t, _headline(d.get("summary"))[0], who, ("Expired, not saved", "neutral", "clock"),
                                             f"at {w.clock(t)}, no answer", str(d.get("token")), False))
        undone = {str(r.e.get("source"))[5:] for r in self.feed.changes if str(r.e.get("source", "")).startswith("undo:") and r.e.get("outcome") == "done"}
        for r in self.changes_in(pick, now - 7 * 86400):
            e = r.e
            who = self.assistants.of(e)
            badge = w.change_badge(e.get("outcome"))
            if e.get("outcome") == "rejected":
                how = f"by {w.windows_user(e.get('user'))}, in the monitor"
            elif e.get("approved_in") == "monitor":
                how = f"approved by {w.windows_user(e.get('user'))} in the monitor"
            else:
                how = f"approved in {who.short}'s chat" if not who.unknown else "approved in the assistant's chat"
            answered.append(Answered(r.t, _headline(e.get("summary"))[0], who, badge, how, str(e.get("token")),
                                     e.get("outcome") == "done" and e.get("action") in ("add", "update") and str(e.get("token")) not in undone))
        waiting.sort(key=lambda a: a.created)
        expired.sort(key=lambda a: a.expires, reverse=True)
        answered.sort(key=lambda a: a.t, reverse=True)
        return waiting, expired, answered

    def _approval(self, folder: Folder, d: dict, pc: Config, now: float) -> Approval:
        who = self.assistants.of(d)
        title, sentence = _headline(d.get("summary"))
        lines = []
        for line in str(d.get("summary") or "").split("\n")[1:]:
            line = line.lstrip("- ").strip()
            m = AMOUNT_LINE.fullmatch(line)
            lines.append((w.dates(m[1]), m[2]) if m else (w.dates(line), ""))
        total = d.get("total")
        sample = is_sample(d.get("company_file"))
        expires = _num(d.get("expires"))
        expired = expires < now
        can, why = True, ""
        action, entity = str(d.get("action")), str(d.get("entity"))
        if folder.sample:
            can, why = False, "This is the monitor's sample data: nothing is saved from it."
        elif expired:
            can, why = False, "It expired, so it cannot be saved. Ask the assistant to prepare it again if it is still needed."
        elif not sample and not pc.allow_writes:
            can, why = False, ("Changes are turned off on this PC (QBD_ALLOW_WRITES), so it can't be saved from here. Answer in "
                               f"{who.short}'s chat, or turn changes on (see Safety).")
        elif not sample and action in ("delete", "void") and not pc.allow_delete:
            can, why = False, "Deletes and voids are turned off on this PC (QBD_ALLOW_DELETE), so it can't be saved from here."
        elif not sample and action != "batch" and entity not in pc.write_entities:
            can, why = False, f"Changes to {entity.replace('_', ' ')} records are not allowed on this PC (QBD_WRITE_ENTITIES)."
        elif not sample and isinstance(total, (int, float)) and total > pc.max_amount:
            can, why = False, f"Its total is above this PC's limit of {w.money(pc.max_amount)} (QBD_MAX_AMOUNT)."
        settings = d.get("settings") if isinstance(d.get("settings"), dict) else {}
        differs = self._differs(settings, pc) if settings and not sample else ""
        return Approval(folder, str(d.get("token")), d, who, sample, title, sentence, lines,
                        w.money(total) if isinstance(total, (int, float)) else "", self.currency(d.get("company_file")),
                        [w.dates(str(x)) for x in d.get("warnings") or []], [w.dates(str(x)) for x in d.get("checks") or []],
                        str(d.get("reason") or ""), w.mask(d.get("session_title") or ""), _num(d.get("created")) or (expires - 900),
                        expires, expired, can, why, f"Drafted with other settings than this PC's: {differs}." if differs else "",
                        _seen(folder, d))

    # ------------------------------------------------------------ changes (CH-1 to CH-4)

    def changes(self, pick: str, since: float) -> list[ChangeRow]:
        return [self.change_row(r) for r in reversed(self.changes_in(pick, since)) if r.e.get("outcome") != "rejected"]

    def change_of(self, token: str) -> ChangeRow | None:
        """A saved change by its token, for Undo from the answered list."""
        r = next((r for r in reversed(self.feed.changes) if str(r.e.get("token")) == token and r.e.get("outcome") == "done"), None)
        return self.change_row(r) if r is not None else None

    def change_row(self, r: Rec) -> ChangeRow:
        e = r.e
        who = self.assistants.of(e)
        title, _ = _headline(e.get("summary"))
        diff = _diff(e) if e.get("action") == "update" else []
        lines = [x.lstrip("- ").strip() for x in str(e.get("summary") or "").split("\n")[1:] if x.strip()]
        n = sum(1 for x in lines if AMOUNT_LINE.fullmatch(x)) or len(lines)
        about = ", ".join(f[0].lower() for f in diff[:3]) if diff else w.plural(n, "line") if lines else ""
        amount = w.money(e["total"]) if isinstance(e.get("total"), (int, float)) else ""
        badge = w.change_badge(e.get("outcome"))
        user = str(e.get("user") or "")
        saved = ""
        if e.get("outcome") == "done":
            how = ("approved in the monitor" if e.get("approved_in") == "monitor" else
                   f"approved in {who.short}'s chat" if not who.unknown else "approved in the assistant's chat")
            saved = f"{w.clock(r.t)} as {user.replace('@', ' on ')}, {how}"
        undo, undo_text = self._undo(r, diff)
        explain = todo = ""
        if e.get("outcome") == "failed":
            m = re.search(r"QuickBooks status (\d+)", str(e.get("error")))
            said = re.sub(r"^QuickBooks status \d+:\s*", "", str(e.get("error") or ""))
            explain = f"QuickBooks refused it: {w.first_sentence(said)} Nothing was saved."
            hint = STATUS_HINTS.get(int(m[1])) if m else None
            todo = f"What to do: {hint or f'ask {who.short} to prepare it again.'}" + (f" QuickBooks status {m[1]}." if m else "")
        elif e.get("outcome") == "unknown":
            explain = "QuickBooks did not confirm this change in time, so it may or may not be saved."
            todo = ("Before anyone tries again, look for it in QuickBooks, or ask the assistant to search for it. Trying again "
                    "without checking could save it twice.")
        return ChangeRow(r, who, title, about, amount, badge, str(e.get("reason") or ""), saved, diff, undo, undo_text, explain, todo)

    def _undo(self, r: Rec, diff: list) -> tuple[bool, str]:
        """Whether Undo can be offered, and what it does or why not (CH-3). qbd itself makes the final check."""
        e = r.e
        if e.get("outcome") != "done":
            return False, ""
        token = str(e.get("token"))
        again = next((c for c in self.feed.changes if c.e.get("source") == f"undo:{token}" and c.e.get("outcome") == "done"), None)
        if again is not None:
            return False, f"Undone at {w.when(again.t, self.now())}."
        if is_sample(e.get("companyFile")):
            pc_writes, pc_delete = True, True
        else:
            pc = self.pc()
            pc_writes, pc_delete = pc.allow_writes, pc.allow_delete
        action = e.get("action")
        if action in ("delete", "void"):
            return False, "Undo isn't possible here: a deleted or voided record can't be put back from qbd. The write log keeps it as it was."
        if not pc_writes:
            return False, "Undo isn't possible here: changes are turned off on this PC."
        if action == "add":
            if not pc_delete:
                return False, "Undo isn't possible here: undoing an added record deletes it, and deletes are not allowed on this PC."
            return True, f"Prepares the deletion of this {str(e.get('entity', 'record')).replace('_', ' ')}, to approve under Approvals."
        if action == "update":
            if len(diff) == 1 and diff[0][1]:
                return True, f"Prepares the change back to \"{diff[0][1]}\", to approve under Approvals."
            return True, "Prepares the change back to the values before, to approve under Approvals."
        return False, ""

    # ------------------------------------------------------------ the Overview (OV-1 to OV-5)

    def overview(self, pick: str) -> Overview:
        now = self.now()
        today = self.midnight()
        calls = self.calls(pick, today)
        changes = self.changes_in(pick, today)
        running, waiting = self.live(pick)
        waiting_drafts, _, _ = self.approvals(pick)
        assistants = {self.assistants.of(r.e).key for r in calls}
        failed = [r for r in calls if r.e.get("outcome") != "ok"]
        not_saved = _not_saved(changes, failed)
        look = len(failed) + len(not_saved)
        tiles = [
            ("Requests today", w.num(len(calls)),
             " · ".join(b for b in [f"by {w.plural(len(assistants), 'assistant')}" if calls else "",
                                    "1 more running now" if running else "", f"{len(waiting)} in line" if waiting else ""] if b)
             or "none yet", ""),
            ("Records read", w.num(sum(r.e.get("items", 0) for r in calls if isinstance(r.e.get("items"), int))), "sent back by QuickBooks", ""),
            ("Changes saved", w.num(sum(1 for r in changes if r.e.get("outcome") == "done")),
             f"{len(waiting_drafts)} more waiting for you" if waiting_drafts else "", ""),
            ("Needs a look", w.num(look), self._look_note(failed, not_saved, pick), "warn" if look else ""),
        ]
        needs: list[dict] = [{"kind": "draft", "a": a} for a in waiting_drafts]
        blocked = self._unsettled(pick, BLOCKED)
        if blocked is not None:
            needs.append({"kind": "blocked", "rec": blocked, "who": self.assistants.of(blocked.e)})
        sessions = [s for s in self.sessions(pick, today)]
        quiet = ""
        if not calls and not changes:
            last = next((r for r in reversed(self.feed.calls) if self._in(self._call_file(r), pick) and r.e.get("command") not in NOT_REQUESTS), None)
            quiet = ("Quiet so far today." + (f" Last request {w.ago(last.t, now)}, by {self.assistants.of(last.e).short}." if last else ""))
        first_run = not self.feed.calls and not self.feed.changes and not self.feed.drafts() and running is None and not waiting
        date = w.local(now)
        return Overview(f"Today, {w.weekday(date)} {date.day} {w.month(date)}", tiles, running, waiting, needs, sessions[:OVERVIEW_SESSIONS],
                        len(sessions), self.banners(pick), quiet, first_run)

    def _look_note(self, failed: list[Rec], not_saved: list[Rec], pick: str) -> str:
        if not failed and not not_saved:
            return "nothing went wrong"
        blocked = [r for r in failed if str(r.e.get("outcome")) in BLOCKED]
        if blocked and len(blocked) == len(failed) and not not_saved:
            solved = self._unsettled(pick, BLOCKED) is None
            return f"{w.plural(len(blocked), 'blocked request')}{', solved since' if solved else ''}"
        bits = []
        if failed:
            bits.append(w.plural(len(failed), "failed request"))
        if not_saved:
            bits.append(f"{w.plural(len(not_saved), 'change')} not saved")
        return ", ".join(bits)

    # ------------------------------------------------------------ the tray (TR-1 to TR-5)

    def tray(self) -> Tray:
        now = self.now()
        running, waiting = self.live(ALL)
        drafts, _, _ = self.approvals(ALL)
        blocked = self._unsettled(ALL, BLOCKED)
        new = self.new_problems()
        closed_try = self._unsettled(ALL, CLOSED)
        closed = self.qb_running is False
        today = self.midnight()
        calls = self.calls(ALL, today)
        saved = sum(1 for r in self.changes_in(ALL, today) if r.e.get("outcome") == "done")
        lines = []
        if waiting:
            lines.append(("clock", "warn", f"{w.num(len(waiting))} waiting in line",
                          " · ".join(f"{c.who.name} {w.span(c.elapsed(now))}" for c in waiting[:3]), "live"))
        if drafts:
            lines.append(("approval", "warn", f"{w.plural(len(drafts), 'change')} {'waits' if len(drafts) == 1 else 'wait'} for your answer",
                          " · ".join(a.title for a in drafts[:3]), "approvals"))
        if blocked is not None:
            lines.append(("window", "warn", "A QuickBooks window is open", f"{blocked.e.get('title') or 'A request'} could not run at "
                          f"{w.clock(blocked.t)}", "problems"))
        if new:
            p = new[0]
            lines.append(("warning", "neutral", f"{w.plural(len(new), 'new problem')}",
                          f"{p.title}, {w.clock(p.last.t)}" + (f" · {p.status[0].lower()}" if p.status[1] == "ok" else ""), "problems"))
        if drafts or blocked is not None:
            state = "waiting"
            tip = (f"{w.plural(len(drafts), 'change')} {'waits' if len(drafts) == 1 else 'wait'} for your answer" if drafts else
                   "waiting for you. A QuickBooks window is open.")
            badge: tuple[str, str, str] | None = ("Waiting for you", "warn", "person")
        elif new or (closed and (closed_try is not None or running is not None or waiting)):
            state = "problem"
            tip = ", ".join(b for b in [w.plural(len(new), "new problem") if new else "", "QuickBooks is closed" if closed else ""] if b)
            badge = ("Problem", "bad", "warning")
        elif running is not None or waiting:
            state = "working"
            c = running or waiting[0]
            tip = f"{c.who.short} is {'reading' if running else 'waiting for'} QuickBooks"
            badge = ("Working", "info", "running")
        else:
            state, tip, badge = "idle", "nothing running", None
        footer = f"Today: {w.plural(len(calls), 'request')} · {w.plural(saved, 'change')} saved"
        return Tray(state, f"Activity Monitor: {tip}", badge, running, lines, footer)

    # ------------------------------------------------------------ the portal

    def last_request(self) -> Rec | None:
        """The newest request in the call logs, leaving out qbd's and the monitor's own lines."""
        return next((r for r in reversed(self.feed.calls) if r.e.get("command") not in NOT_REQUESTS), None)

    def portal(self) -> Portal:
        """What the portal shows: what runs now; else a session stopped, changes waiting for an answer, a request
        blocked by a QuickBooks window, QuickBooks closed, a request still running inside QuickBooks, a command in
        line; else the last request, done."""
        running, waiting = self.live(ALL)
        drafts = self.approvals(ALL)[0]
        line, need = len(waiting), len(drafts)
        if running is not None:
            c = running
            step = f"{c.step}, page {c.page} of {c.pages}" if c.page and c.pages else c.step
            return Portal("stopping" if c.stop_asked else "running", c.since, c.who, c, c.title,
                          " · ".join(b for b in [c.who.name, step] if b), badge=("Running", "info", "pulse"), line=line, need=need)
        paused = self.paused(ALL)
        if paused:
            folder, info, who = max(paused, key=lambda p: w.iso_time(p[1].get("at")) or 0.0)
            step = info.get("step")
            by = w.windows_user(info.get("by"))
            mine = by.casefold() == w.windows_user(who_am_i()).casefold()
            saved = "The records saved before the stop stay saved." if info.get("command") in ("commit", "import") else "Nothing was saved."
            name = who.short if not who.unknown else "The assistant"
            return Portal("stopped", w.iso_time(info.get("at")) or self.now(), who, None,
                          str(info.get("request") or info.get("title") or info.get("command") or "A request"),
                          who.name + (f" · after step {step}" if isinstance(step, int) and step > 0 else ""),
                          f"Stopped by {'you' if mine else by or 'someone'}. {saved} {name}'s next command waits until you resume.",
                          ("Stopped", "neutral", "stop_square"), (folder, info), line=line, need=need)
        if drafts:
            first = min(drafts, key=lambda a: a.expires)
            names = {a.who.name for a in drafts}
            return Portal("waiting", first.created, None, None, f"{w.plural(need, 'change')} {'waits' if need == 1 else 'wait'} for your answer",
                          f"From {next(iter(names)) if len(names) == 1 else 'assistants'}", badge=("Waiting for you", "warn", "person"),
                          expires=first.expires, line=line, need=need, page="approvals")
        blocked = self._unsettled(ALL, BLOCKED)
        if blocked is not None:
            who = self.assistants.of(blocked.e)
            return Portal("blocked", blocked.t, who, None, "A QuickBooks window is open and waiting for someone",
                          f"Close it, then retry. It may be hidden behind other windows. {blocked.e.get('title') or 'A request'} could "
                          f"not run, for {who.name}.", badge=("Blocked by a window", "bad", "window"), line=line, page="problems")
        closed = self._unsettled(ALL, CLOSED)
        if closed is not None and self.qb_running is not True:
            who = self.assistants.of(closed.e)
            return Portal("closed", closed.t, who, None, "QuickBooks is closed",
                          f"{who.name} can't reach the books until someone opens the company file in QuickBooks.",
                          badge=("Problem", "bad", "x"), line=line, page="problems")
        timeout = self._unsettled(ALL, {"timeout"})
        if timeout is not None:
            who = self.assistants.of(timeout.e)
            return Portal("timeout", timeout.t - float(timeout.e.get("ms") or 0) / 1000, who, None, "Still running inside QuickBooks",
                          f"{timeout.e.get('title') or 'A request'}, for {who.name}, gave up after "
                          f"{w.span(float(timeout.e.get('ms') or 0) / 1000)}. QuickBooks may be slow until it finishes; there is no cancel.",
                          badge=("Timed out", "bad", "clock"), line=line)
        if waiting:
            c = waiting[0]
            return Portal("line", c.since, c.who, c, c.title, f"{c.who.name} · waiting for its turn", badge=("In line", "warn", "clock"),
                          line=line)
        last = self.last_request()
        if last is None:
            return Portal("idle")
        who = self.assistants.of(last.e)
        return Portal("done", last.t, who, None, str(last.e.get("title") or last.e.get("command") or "A request"),
                      " · ".join([who.name, w.plural(int(last.e.get("items") or 0), "record"), w.secs(last.e.get("ms"))]),
                      badge=w.call_badge(last.e))

    # ------------------------------------------------------------ the Safety panel (SA-1 to SA-4)

    def safety(self, pick: str) -> dict:
        now = self.now()
        pc = self.pc()
        facts_all = self.feed.facts()
        facts = None
        if pick != ALL:
            facts = next((v for p, v in facts_all.items() if file_key(p) == pick), None)
        if facts is None and facts_all:
            facts = max(facts_all.values(), key=lambda v: str(v.get("read", "")))
        path = next((p for p, v in facts_all.items() if v is facts), "")
        qb = []
        if facts:
            read = w.iso_time(facts.get("read"))
            product = " ".join(x for x in [str(facts.get("product") or ""), str(facts.get("version") or "")] if x)
            country = {"US": "United States", "CA": "Canada", "UK": "United Kingdom", "GB": "United Kingdom"}.get(str(facts.get("country") or ""), str(facts.get("country") or ""))
            closing = facts.get("closing_date")
            qb = [("Company", str(facts.get("company") or "not read yet"), ""), ("Company file", path, ""),
                  ("Product", ", ".join(x for x in [product, country] if x), ""),
                  ("Closing date", w.date_words(closing) if closing else "not set",
                   "changes dated on or before it are refused" if closing else "")]
            qb_note = (f"As qbd last read them, {w.ago(read, now) if read else 'at some point'}"
                       f"{', for ' + str(facts.get('by')) if facts.get('by') else ''}. The monitor never asks QuickBooks itself.")
        else:
            qb_note = "qbd has not read this company file yet. The monitor never asks QuickBooks itself."
        ents = ", ".join(x.replace("_", " ") for x in pc.write_entities)
        changes = [
            ("Changes", "Allowed, each one approved first" if pc.allow_writes else "Not allowed: read-only",
             f"QBD_ALLOW_WRITES={'true' if pc.allow_writes else 'false'}", "ok" if pc.allow_writes else "muted"),
            ("Record types", ents[0].upper() + ents[1:] if ents else "none", "QBD_WRITE_ENTITIES", ""),
            ("Deletes and voids", "Allowed, each one approved first" if pc.allow_delete else "Not allowed",
             f"QBD_ALLOW_DELETE={'true' if pc.allow_delete else 'false'}", "ok" if pc.allow_delete else "muted"),
            ("Largest total", f"{w.money(pc.max_amount)}{' ' + self.currency(path) if self.currency(path) else ''}",
             f"QBD_MAX_AMOUNT={w.num(pc.max_amount).replace(',', '')}", ""),
        ]
        if pc.require_monitor_approval:
            changes.append(("Approval", "Only in this monitor: a commit from a chat is refused", "QBD_REQUIRE_MONITOR_APPROVAL=true", "ok"))
        kept = []
        for f in self.feed.folders:
            kept.append(f"{f.path} · " + ("shared by every Windows account that sets QBD_STATE_DIR" if f.shared else
                                          "this Windows user's own folder"))
        privacy = [
            ("Personal data", "Masked: ID, card and bank numbers show the last 4 digits" if pc.mask_personal_data else
             "Not masked: assistants see full ID, card and bank numbers", f"QBD_MASK_PERSONAL_DATA={'true' if pc.mask_personal_data else 'false'}",
             "ok" if pc.mask_personal_data else "warn"),
            ("Call log", "On: every request, counted not copied" if pc.call_log else "Off: only changes are kept",
             f"QBD_CALL_LOG={'on' if pc.call_log else 'off'}", "ok" if pc.call_log else "warn"),
            ("Write log", "Always on: every change saved or tried, with the values before", "", ""),
            ("Kept in", "\n".join(kept), "on this PC only", ""),
        ]
        cuts = self.cuts(now - KEEP_DAYS * 86400)
        privacy.append(("Logs as evidence", "\n".join(f"{name} changed outside qbd, noticed {w.when(t, now)} ({folder})" for t, name, folder in cuts)
                        if cuts else "Only added to by qbd, as far as the monitor has seen", "qbd never shortens a log", "bad" if cuts else "ok"))
        quick = [
            ("One request", f"Gives up after {w.span(pc.timeout_seconds)}; QuickBooks may finish it anyway",
             f"QBD_TIMEOUT_SECONDS={w.num(pc.timeout_seconds).replace(',', '')}", ""),
            ("Waiting in line", f"Up to {min(int(pc.timeout_seconds), LINE_WAIT)} s, first come first served, then \"busy\", "
             "safe to retry", "", ""),
            ("Reads", "Searches return 25 records, 1,000 at most; checks read 5,000 to 20,000 records per step at most, and say "
             "when they stop short", "", ""),
        ]
        warning = None
        week = [s for s in self.sessions(pick, now - 7 * 86400)]
        odd = []
        for s in week:
            used = [r.e.get("settings") for r in s.calls if isinstance(r.e.get("settings"), dict)]
            differs = self._differs(used[-1], pc) if used else ""
            if differs:
                deletes = [c for c in s.changes if c.e.get("action") in ("delete", "void") and c.e.get("outcome") == "done"]
                odd.append((s, differs, deletes))
        if odd:
            s, differs, deletes = odd[0]
            more = f" and {w.plural(len(odd) - 1, 'more')}" if len(odd) > 1 else ""
            warning = Banner("settings", "warn", "Other settings used",
                             f"{w.plural(len(odd), 'session')} this week ran with settings different from this PC's: {s.who.name} on "
                             f"{w.short_day(s.start, now)} had {differs}{more}.",
                             "An assistant can set these in its own command line. " +
                             ("No delete was saved in that session." if "deletes" in differs and not deletes else
                              f"{w.plural(len(deletes), 'delete')} saved in that session." if deletes else ""),
                             "session", s.sid)
        return {"warning": warning, "quickbooks": qb, "quickbooks_note": qb_note, "changes": changes, "privacy": privacy,
                "quick": quick}

    # ------------------------------------------------------------ for Technical details

    def skipped_text(self) -> str:
        skipped = self.feed.skipped()
        if not skipped:
            return ""
        s = skipped[-1]
        after = f", after the entry of {w.when(s.after, self.now())}" if s.after else ""
        return (f"{w.plural(len(skipped), 'line')} in the logs could not be read and {'was' if len(skipped) == 1 else 'were'} "
                f"skipped. The last: {s.src}, line {w.num(s.line)}{after}.")


# ---------------------------------------------------------------- helpers


def _not_saved(changes: list[Rec], failed: list[Rec]) -> list[Rec]:
    """Changes not saved, less those whose failed commit is already counted as a failed request."""
    counted = {str(r.e["args"].get("token")) for r in failed if r.e.get("command") == "commit" and isinstance(r.e.get("args"), dict)}
    return [r for r in changes if r.e.get("outcome") in ("failed", "unknown") and str(r.e.get("token")) not in counted]


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _common_start(words: list[str]) -> str:
    """The text all the words start with (Python 3.15 deprecates os.path.commonprefix, which did this)."""
    lo, hi = min(words), max(words)
    n = 0
    while n < len(lo) and lo[n] == hi[n]:
        n += 1
    return lo[:n]


def _is_draft(name: str) -> bool:
    return name.endswith(".json") and not name.endswith((".answer.json", ".expired.json"))


def _token_of(name: str) -> str | None:
    """The token a drafts-folder file is filed under, when it is one qbd writes."""
    for end in (".answer.json", ".expired.json", ".json"):
        if name.endswith(end):
            token = name[: -len(end)]
            return token if TOKEN.match(token) else None
    return None


def _seen(folder: Folder, d: dict) -> str:
    """The fingerprint of a draft as shown: the same as qbd's Drafts.fingerprint of the files read."""
    digests = folder.drafts.digests if folder.drafts is not None and folder.drafts.digests is not None else {}
    own = digests.get(f"{d.get('token')}.json")
    if not own:
        return ""
    records = batch_tokens(d.get("request")) if d.get("action") == "batch" else []
    return fingerprint([own, *(digests.get(f"{t}.json") or "-" for t in records)])


def _headline(summary: Any) -> tuple[str, str]:
    """A draft's first line as a title and a sentence: "Add bill for Coastal Freight Lines" and the whole line with
    its dates in words."""
    first = str(summary or "").split("\n")[0].strip()
    title = re.split(r" dated | on \d{4}-| \(|, ", first, maxsplit=1)[0].strip() or first
    return title, w.dates(first)


def _plain(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(_plain(x) for x in v.values() if x not in (None, ""))
    if isinstance(v, list):
        return ", ".join(_plain(x) for x in v)
    return "" if v is None else str(v)


def _diff(e: dict) -> list[tuple[str, str, str]]:
    """Field, before and after of an update, from the values the write log kept and its (masked) request."""
    before = e.get("before") if isinstance(e.get("before"), dict) else {}
    try:
        root = ET.fromstring(str(e.get("request") or ""))
    except ET.ParseError:
        return []
    mod = next((x for x in root.iter() if x.tag.endswith("Mod") and x.tag != root.tag), None)
    if mod is None:
        return []
    out = []
    for child in mod:
        if child.tag in ("ListID", "TxnID", "EditSequence") or child.tag.endswith("LineMod"):
            continue
        after = ", ".join(t.strip() for t in child.itertext() if t.strip()) if len(child) else (child.text or "").strip()
        was = _plain(before.get(child.tag))
        if after != was:
            out.append((w._split(child.tag.replace("Ref", "")), was, after))
    return out
