"""The screens: Overview, Live, Sessions and one session, Approvals, Changes, Problems and Safety. Each shows a view the
model built; a section is built again only when what it shows changed, and times that follow the clock tick on their
own."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import wx

from .. import words as w
from ..model import ALL, LINE_WAIT, Answered, Approval, Banner as BannerView, ChangeRow, LiveCmd, ProblemRow, SessionRow
from .base import (Page, card_head, change_item, finished_row, group_head, holder, line_up, problem_item, request_item,
                   running_card_body, session_row, waiting_row)
from .widgets import (Avatar, Badge, Banner, Bubble, Dropdown, Field, Grid, Icon, IconTile, Node, Row, Section, Segments, Tile, box,
                      button, card, divider, elided, fixed, grid, hbox, icon_button, label, link, pair, px, vbox)

if TYPE_CHECKING:
    from .app import Controller

PERIODS = [(1, "Today"), (7, "Last 7 days"), (30, "Last 30 days"), (90, "Last 90 days")]
SHOW = 25  # sessions shown at a time
ENTITY_ICONS = {"deposit": "deposit", "bill": "receipt", "invoice": "document", "receive_payment": "deposit"}


# ---------------------------------------------------------------- signatures: what a section shows, to tell a change


def live_sig(c: LiveCmd | None) -> Any:
    if c is None:
        return None
    return (c.pid, c.running, c.title, c.args, c.session_title, c.step, c.doing, c.page, c.pages, c.read, c.total, c.percent,
            c.capped, c.cap, c.report, c.stop_asked, c.stoppable, tuple(c.done), round(c.since, 1), c.who.key, c.who.colour)


def session_sig(s: SessionRow) -> Any:
    return s.sid, s.asked, s.start, s.end, s.counts_text(), s.badge, s.who.key, s.who.colour


def approval_sig(a: Approval) -> Any:
    return a.token, a.seen, a.can_approve, a.why_not, a.expired, a.note, a.who.colour


def banner_sig(b: BannerView) -> Any:
    return b.kind, b.title, b.text, b.more, b.action


def change_sig(c: ChangeRow) -> Any:
    return c.rec.src, c.rec.line, c.badge, c.undo, c.undo_text, c.who.colour


def problem_sig(p: ProblemRow, new: bool) -> Any:
    return p.pid, p.count, p.status, new, p.last.t, p.solved, p.who.colour


def since_of(app: "Controller", days: int) -> float:
    """The start of a period of calendar days: today from midnight, the last 7 days from midnight 6 days ago."""
    return app.model.midnight() - (days - 1) * 86400


def period_words(days: int) -> str:
    return "today" if days == 1 else f"in the last {days} days"


# ---------------------------------------------------------------- pieces several pages use


def go(text: str, fn: Callable[[], None], icon: str = "chevron_right") -> Node:
    return link(text, icon, fn)


def padded(node: Node, pad: tuple) -> Node:
    b = vbox(pad)
    b.add(node)
    return b


def banner_widget(b: BannerView, app: "Controller") -> Node:
    action: Node | None = None
    if b.action in ("live", "safety", "problems"):
        action = go({"live": "Live view", "safety": "Safety", "problems": "Problems"}[b.action], lambda: app.show(b.action))
    elif b.action == "resume":
        folder, info = b.data
        action = button(f"Resume {w.cut(str(info.get('name') or 'the assistant'), 30)}", "", "play", on_click=lambda: app.resume(folder, info))
        action.key = f"resume:{info.get('agent')}|{info.get('session')}|{info.get('program')}"
        action.enabled = not app.busy_with(f"resume:{folder.path}:{info.get('agent')}:{info.get('session')}:{info.get('program')}")
    elif b.action == "session":
        action = go("See the session", lambda: app.open_session(b.data))
    icon = {"closed": "x", "blocked": "window", "timeout": "clock", "busy": "clock", "paused": "pause", "call_log_off": "info",
            "log_cut": "warning", "settings": "warning"}.get(b.kind, "info")
    return Banner(b.tone, icon, b.title, b.text, b.more, action)


def banners_box(banners: list[BannerView], app: "Controller") -> Node | None:
    if not banners:
        return None
    col = vbox(gap=10)
    for b in banners:
        col.add(banner_widget(b, app))
    return col


def table_head(columns: list[tuple[str, int]], left: int = 20) -> Node:
    """Column names over a list; a width in pixels, or 0 to take the rest."""
    f = box("rowBare", (left, 8, 20, 8), 12, horizontal=True)
    for text, width in columns:
        lab = label(text, "caption", "secondary")
        if width:
            f.add(fixed(lab, width))
        else:
            f.add(lab, 1)
    return f


def card_title(title: str, count: int | None = None, more: str = "", action: Node | None = None,
               margins: tuple = (20, 18, 20, 14)) -> Node:
    return padded(card_head(title, count, more, action), margins)


def empty(text: str, pad: tuple = (20, 4, 20, 18)) -> Node:
    return padded(label(text, "body", "secondary", wrap=True), pad)


def two_columns(narrow: bool, a: Node, b: Node, gap: tuple = (20, 32)) -> Node:
    """Side by side, or one under the other on a narrow window."""
    if narrow:
        col = vbox(gap=gap[0])
        col.add(a)
        col.add(b)
        return col
    row = hbox(gap=gap[1])
    row.add(a, 1, align="top")
    row.add(b, 1, align="top")
    return row


# ---------------------------------------------------------------- Overview (OV-1 to OV-6)


class OverviewPage(Page):
    name = "overview"
    title = "Overview"
    date_line = ""

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.mode = Section()
        self.right.insert(1, self.mode, align="top")
        self.first = self.section()
        self.banners = self.section()
        self.tiles = self.section()
        self.middle = self.section()
        self.today = self.section()
        self.note = label("Only names, counts and times are shown here. Nothing on this page comes from QuickBooks' answers, and "
                          "nothing leaves this PC.", "caption", "secondary", wrap=True)
        self.col.add(self.note)
        self.end()
        self.ticker.add(self.sub, lambda: f"{self.date_line} · updated {w.ago(self.app.updated, self.app.now())}")

    def refresh(self) -> None:
        super().refresh()
        m, pick, e = self.app.model, self.app.pick, (self.app.epoch, self.narrow)
        o = m.overview(pick)
        self.date_line = o.date_line
        pc = m.pc()
        self.mode.show_built((e, pc.allow_writes, pc.require_monitor_approval),
                             lambda: self.build_mode(pc.allow_writes) if not self.narrow else None)
        if o.first_run:
            self.first.show_built((e, "first"), self.build_first)
            for s in (self.banners, self.tiles, self.middle, self.today):
                s.show_built(None, lambda: None)
            self.note.show(False)
            return
        self.first.show_built(None, lambda: None)
        self.note.show(True)
        self.banners.show_built((e, tuple(banner_sig(b) for b in o.banners), tuple(self.app.busy)), lambda: banners_box(o.banners, self.app))
        self.tiles.show_built((e, tuple(o.tiles)), lambda: self.build_tiles(o.tiles))
        needs = tuple((n["kind"], approval_sig(n["a"]) if n["kind"] == "draft" else (n["rec"].src, n["rec"].line)) for n in o.needs)
        self.middle.show_built((e, live_sig(o.running), tuple(live_sig(c) for c in o.waiting), needs, o.quiet, tuple(self.app.busy)),
                               lambda: self.build_middle(o))
        self.today.show_built((e, tuple(session_sig(s) for s in o.sessions), o.sessions_today), lambda: self.build_today(o))

    def build_mode(self, writes: bool) -> Node:
        b = button("Changes need your approval" if writes else "Read-only: no changes", "", "shield",
                   tooltip="How assistants may work in QuickBooks from this PC", on_click=lambda: self.app.show("safety"))
        b.h = 36
        b.key = "mode"
        return b

    def build_first(self) -> Node:
        f = card((28, 28, 28, 28), 10)
        f.add(label("Nothing to show yet", "subtitle"))
        f.add(label("When an assistant works in QuickBooks through qbd on this PC, what it reads and changes shows here. "
                    "No assistant has done that yet.", "body", wrap=True))
        f.add(label('So each one shows by name, set QBD_AGENT in its settings, for example "Claude (Claude Code)".',
                    "body", "secondary", wrap=True))
        f.add(go("Safety settings", lambda: self.app.show("safety")), align="left")
        return f

    def build_tiles(self, tiles: list[tuple[str, str, str, str]]) -> Node:
        cols = 2 if self.narrow else 4
        g = grid(cols, 12)
        g.row_fill = True
        targets: list[Callable[[], None]] = [lambda: self.app.show("sessions", period=1), lambda: self.app.show("sessions", period=1),
                                             lambda: self.app.show("changes"), lambda: self.app.show("problems")]
        for i, (title, value, note, tone) in enumerate(tiles):
            t = Tile(title, value, note, tone, "warning" if tone == "warn" else "", on_click=targets[i])
            t.key = f"tile:{i}"
            g.put(t, i // cols, i % cols)
        for c in range(cols):
            g.grow_col(c)
        return g

    def build_middle(self, o: Any) -> Node:
        return two_columns(self.narrow, self.build_running(o), self.build_needs(o), (16, 16))

    def build_running(self, o: Any) -> Node:
        f = card((20, 20, 20, 20), 14)
        f.add(card_head("Running now", action=go("Live view", lambda: self.app.show("live"))))
        if o.running is not None:
            f.add(running_card_body(o.running, self.app, self.ticker, big=False))
        else:
            f.add(label("Nothing is talking to QuickBooks right now." + (f" {o.quiet}" if o.quiet else ""), "body", "secondary", wrap=True))
        if o.waiting:
            line = f.add(box("rowPlain", (0, 14, 0, 0), 8))
            line.add(label(f"Waiting in line · {len(o.waiting)}", "caption_strong", "secondary"))
            for c in o.waiting:
                line.add(waiting_row(c, self.app, self.ticker, boxed=False))
            line.add(label(f"Each waits up to {LINE_WAIT} s for its turn, then gives up and can try again.", "caption", "secondary",
                           wrap=True))
        return f

    def build_needs(self, o: Any) -> Node:
        f = card((20, 20, 20, 20), 12)
        f.add(card_head("Needs you", len(o.needs) or None, action=go("Approvals", lambda: self.app.show("approvals"))))
        for n in o.needs:
            f.add(self.need_draft(n["a"]) if n["kind"] == "draft" else self.need_blocked(n["rec"], n["who"]))
        if not o.needs:
            f.add(label("Nothing waits for you.", "body", "secondary"))
        f.add(label("Nothing is saved until you approve it. A request held up by a QuickBooks window also shows here.",
                    "caption", "secondary", wrap=True))
        return f

    def need_draft(self, a: Approval) -> Node:
        r = Row("needItem", (14, 12, 14, 12), 12, on_click=lambda: self.app.show("approvals", token=a.token))
        r.key = f"need:{a.token}"
        r.name = f"{a.title}. {a.brief}. Waiting for your answer."
        r.add(IconTile("approval", "warn"), align="top")
        col = vbox(gap=2)
        col.add(label(a.title, "strong", wrap=True))
        col.add(label(a.brief, "body", "secondary", wrap=True))
        status = hbox(gap=4)
        if a.flag:
            status.add(Icon("warning", "warn", 12))
            status.add(label(a.flag, "caption", "warn"))
            status.space(8)
        status.add(Icon("clock", "warn", 12))
        status.add(self.ticker.add(elided("", "caption", "warn"), lambda: (
            ("Waiting for your answer · expires" if not a.flag else "Expires") + f" in {w.span(max(0.0, a.expires - self.app.now()))}"
            if a.expires > self.app.now() else "Expired")), 1)
        col.add(status)
        r.add(col, 1)
        r.add(button("Review", on_click=lambda: self.app.show("approvals", token=a.token)))
        return r

    def need_blocked(self, rec: Any, who: Any) -> Node:
        r = Row("needItem", (14, 12, 14, 12), 12, on_click=lambda: self.app.show("problems"))
        r.key = f"need:{rec.src}:{rec.line}"
        r.name = "A QuickBooks window is open"
        r.add(IconTile("window", "warn"), align="top")
        col = vbox(gap=2)
        col.add(label("A QuickBooks window is open", "strong"))
        col.add(label(f"{rec.e.get('title') or 'A request'} could not run at {w.clock(rec.t)} · from {who.name}", "body", "secondary",
                      wrap=True))
        col.add(label("Close it in QuickBooks, then ask for a retry.", "caption", "warn", wrap=True))
        r.add(col, 1)
        return r

    def build_today(self, o: Any) -> Node:
        f = card((0, 0, 0, 0), 0)
        f.add(card_title("Today's sessions", None, w.plural(o.sessions_today, "session"),
                         go("All sessions", lambda: self.app.show("sessions", period=1))))
        for s in o.sessions:
            f.add(session_row(s, self.app))
        if not o.sessions:
            f.add(empty("No sessions yet today."))
        elif o.sessions_today > len(o.sessions):
            more = box("rowPlain", (20, 10, 20, 12), 0, horizontal=True)
            more.add(go(f"Show all {o.sessions_today}", lambda: self.app.show("sessions", period=1)))
            more.spring()
            f.add(more)
        return f


# ---------------------------------------------------------------- Live (LV-1 to LV-5)


class LivePage(Page):
    name = "live"
    title = "Live"
    subtitle = "One request at a time talks to QuickBooks. This page follows it as it runs."

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.banners = self.section()
        self.middle = self.section()
        self.finished = self.section()
        self.end()

    def refresh(self) -> None:
        super().refresh()
        m, pick, e = self.app.model, self.app.pick, (self.app.epoch, self.narrow)
        running, waiting = m.live(pick)
        banners = [b for b in m.banners(pick) if b.kind in ("closed", "blocked", "timeout", "paused")]
        self.banners.show_built((e, tuple(banner_sig(b) for b in banners), tuple(self.app.busy)), lambda: banners_box(banners, self.app))
        self.middle.show_built((e, live_sig(running), tuple(live_sig(c) for c in waiting), tuple(self.app.busy)),
                               lambda: self.build_middle(running, waiting))
        done = list(reversed(m.calls(pick, m.midnight())))[:5]
        self.finished.show_built((e, tuple((r.src, r.line) for r in done)), lambda: self.build_finished(done))

    def build_middle(self, running: LiveCmd | None, waiting: list[LiveCmd]) -> Node:
        return two_columns(self.narrow, self.build_running(running), self.build_waiting(waiting), (16, 16))

    def build_running(self, c: LiveCmd | None) -> Node:
        f = card((20, 18, 20, 20), 14)
        head = hbox(gap=10)
        head.add(label("Running now", "subtitle"))
        if c is not None:
            head.add(Badge("Running", "info", "running"))
            head.spring()
            head.add(self.ticker.add(label("", "body", "secondary"),
                                     lambda: f"started {w.clock(c.since, seconds=True)} · {w.span(c.elapsed(self.app.now()))}"))
        else:
            head.spring()
        f.add(head)
        if c is None:
            f.add(label("Nothing is talking to QuickBooks right now.", "body", "secondary", wrap=True))
            return f
        f.add(running_card_body(c, self.app, self.ticker, big=True))
        return f

    def build_waiting(self, waiting: list[LiveCmd]) -> Node:
        f = card((20, 18, 20, 18), 12)
        f.add(card_head("Waiting in line", len(waiting) or None))
        f.add(label(f"In the order they go: first come, first served. Each waits up to {LINE_WAIT} s for its turn, then "
                    "gives up and can try again.", "body", "secondary", wrap=True))
        for c in waiting:
            f.add(waiting_row(c, self.app, self.ticker, boxed=True))
        if not waiting:
            f.add(label("No one is waiting.", "body", "secondary"))
        return f

    def build_finished(self, done: list[Any]) -> Node:
        f = card((0, 0, 0, 0), 0)
        f.add(card_title("Just finished", action=go("All of today's requests", lambda: self.app.show("sessions", period=1))))
        rows = [finished_row(r, self.app) for r in done]
        line_up(rows)
        for row in rows:
            f.add(row)
        if not done:
            f.add(empty("No requests yet today."))
        return f


# ---------------------------------------------------------------- Sessions (SE-1 to SE-3)


class SessionsPage(Page):
    name = "sessions"
    title = "Sessions"

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.query, self.who, self.period, self.limit = "", ALL, 7, SHOW
        controls = hbox(gap=12)
        self.search = Field("Search by assistant or request", self.searched, name="Search sessions by assistant or request", most=420)
        controls.add(self.search, 1)
        self.who_box = controls.add(Section())
        self.period_box = controls.add(Section())
        self.col.add(controls)
        self.table = self.section()
        self.foot('Hover a long request for the whole text. "Unknown assistant" is a caller that gave no name; it shows with its '
                  "Windows user.")
        self.end()

    def searched(self, text: str) -> None:
        self.query, self.limit = text.strip().casefold(), SHOW
        self.refresh()

    def set_period(self, days: int) -> None:
        self.period, self.limit = days, SHOW
        self.refresh()

    def set_who(self, key: str) -> None:
        self.who, self.limit = key, SHOW
        self.refresh()

    def refresh(self) -> None:
        super().refresh()
        m, e = self.app.model, (self.app.epoch, self.narrow)
        rows = m.sessions(self.app.pick, since_of(self.app, self.period))
        people = sorted({s.who.key: s.who for s in rows}.values(), key=lambda x: x.name.casefold())
        if self.who != ALL and self.who not in {p.key for p in people}:
            people.append(next((s.who for s in m.sessions(self.app.pick, 0) if s.who.key == self.who), None) or people[0] if people else None)  # type: ignore[arg-type]
        self.who_box.show_built((e, self.who, tuple((p.key, p.name) for p in people if p)), lambda: self.who_menu([p for p in people if p]))
        self.period_box.show_built((e, self.period), self.period_menu)
        shown = [s for s in rows if (self.who == ALL or s.who.key == self.who) and
                 (not self.query or self.query in f"{s.who.name} {s.who.user} {s.asked}".casefold())]
        noun = w.plural(len(shown), "session")
        self.sub.set_text(f"{noun} {period_words(self.period)}" + (f" matching “{self.search.value().strip()}”" if self.query else "")
                          + ". A session is one assistant's conversation or run.")
        page = shown[: self.limit]
        self.table.show_built((e, tuple(session_sig(s) for s in page), len(shown), self.limit, self.query),
                              lambda: self.build_table(page, shown))

    def who_menu(self, people: list[Any]) -> Node:
        d = Dropdown([(ALL, "All assistants", "")] + [(p.key, p.name, p.user if p.unknown else "") for p in people], self.who,
                     on_pick=self.set_who)
        d.key = "who"
        return d

    def period_menu(self) -> Node:
        d = Dropdown([(days, text, "") for days, text in PERIODS], self.period, on_pick=self.set_period)
        d.key = "period"
        return d

    def build_table(self, page: list[SessionRow], shown: list[SessionRow]) -> Node:
        f = card((0, 0, 0, 0), 0)
        now = self.app.now()
        built = {s.sid: session_row(s, self.app, columns=not self.narrow) for s in page}
        result = (line_up(list(built.values())) or [px(112)])[0]
        if not self.narrow:
            f.add(table_head([("", px(24)), ("Assistant", px(130)), ("What it was asked", 0), ("Time", px(96)),
                              ("Requests and records", px(236)), ("Result", result)]))
        if not shown:
            f.add(empty("No sessions match." if self.query or self.who != ALL else f"No sessions {period_words(self.period)}.",
                        (20, 16, 20, 18)))
            return f
        days: dict[str, list[SessionRow]] = {}
        for s in shown:
            days.setdefault(w.day(s.start, now), []).append(s)
        on_page = {s.sid for s in page}
        first = self.narrow
        for day, rows in days.items():
            here = [s for s in rows if s.sid in on_page]
            if not here:
                continue
            more = w.plural(len(rows), "session") if len(here) == len(rows) else f"{len(here)} of {w.plural(len(rows), 'session')}"
            f.add(group_head(day, more, first))
            first = False
            for s in here:
                f.add(built[s.sid])
        foot = box("rowPlain", (20, 12, 20, 12), 12, horizontal=True)
        foot.add(label(f"Showing {len(page)} of {len(shown)}", "body", "secondary"), 1)
        if len(shown) > len(page):
            more_b = button(f"Show {min(SHOW, len(shown) - len(page))} more", on_click=self.more)
            more_b.key = "more"
            foot.add(more_b)
        f.add(foot)
        return f

    def more(self) -> None:
        self.limit += SHOW
        self.refresh()


# ---------------------------------------------------------------- one session (SE-4 to SE-6)


class SessionPage(Page):
    name = "session"
    title = ""
    uses_picker = False

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.sid = ""
        self.opened: set[str] = set()
        self.head.remove(self.left)
        self.crumbs = Section()
        self.head.insert(0, self.crumbs, 1)
        self.header = self.section()
        self.requests = self.section()
        self.changes = self.section()
        self.foot("Durations are how long each request took, start to finish. Times are this PC's local time; hover a time for the "
                  "full date.")
        self.end()

    def open(self, sid: str, request: str = "") -> None:
        if sid != self.sid:
            self.opened = set()
        self.sid = sid
        if request:
            self.opened.add(request)
        self.refresh()
        self.layout_now()
        self.to_top()

    def refresh(self) -> None:
        super().refresh()
        e = (self.app.epoch, self.narrow)
        d = self.app.model.session(self.sid, ALL) if self.sid else None
        if d is None:
            self.crumbs.show_built((e, None), lambda: self.build_crumbs(None))
            self.header.show_built((e, None), lambda: label("This session is no longer in the logs the monitor reads.", "body", "secondary"))
            self.requests.show_built(None, lambda: None)
            self.changes.show_built(None, lambda: None)
            return
        row = d.row
        self.crumbs.show_built((e, row.sid, row.who.name, row.start), lambda: self.build_crumbs(row))
        self.header.show_built((e, session_sig(row), d.span, tuple(d.totals), d.settings_note), lambda: self.build_header(d))
        self.requests.show_built((e, tuple((q.rec.src, q.rec.line, q.badge) for q in d.requests), d.longest),
                                 lambda: self.build_requests(d))
        self.changes.show_built((e, tuple(change_sig(c) for c in d.changes)), lambda: self.build_changes(d))

    def build_crumbs(self, row: SessionRow | None) -> Node:
        line = hbox(gap=6)
        line.add(go("Sessions", lambda: self.app.show("sessions"), ""))
        if row is None:
            line.spring()
            return line
        now = self.app.now()
        line.add(Icon("chevron_right", "secondary", 12))
        days = max(1, int((self.app.model.midnight() - w.local(row.start).replace(hour=0, minute=0, second=0).timestamp()) // 86400) + 1)
        period = next((p for p, _ in PERIODS if p >= days), 90)
        line.add(go(w.day(row.start, now), lambda: self.app.show("sessions", period=period), ""))
        line.add(Icon("chevron_right", "secondary", 12))
        line.add(elided(f"{row.who.name}, {w.clock(row.start)}", "body"), 1)
        return line

    def build_header(self, d: Any) -> Node:
        row = d.row
        f = card((24, 20, 24, 20), 16)
        top = hbox(gap=16)
        top.add(Avatar(row.who, 40), align="top")
        col = vbox(gap=4)
        line = hbox(gap=8)
        line.add(label(row.who.name, "strong"))
        if row.who.unknown and row.who.user:
            line.add(label(f"· {row.who.user}", "body", "secondary"))
        if row.session:
            line.add(elided(f"session {row.session}", "body", "secondary"))
        line.add(Badge(*row.badge))
        line.spring()
        col.add(line)
        col.add(label("What it was asked" if row.titled else "What it did", "caption", "secondary"))
        col.add(label(row.asked, "subtitle", wrap=True, select=True))
        col.add(label(d.span, "body", "secondary", wrap=True))
        top.add(col, 1)
        f.add(top)
        cols = 3 if self.narrow else 5
        g = grid(cols, 12)
        g.row_fill = True
        for i, (name, value) in enumerate(d.totals):
            g.put(Tile(name, value, small=True), i // cols, i % cols)
        for c in range(cols):
            g.grow_col(c)
        f.add(g)
        text, tone = d.settings_note
        if text:
            note = hbox(gap=8)
            note.add(Icon("shield", tone or "secondary", 14), align="top")
            note.add(label(text, "caption", tone or "secondary", wrap=True), 1)
            f.add(note)
        return f

    def build_requests(self, d: Any) -> Node:
        f = card((0, 0, 0, 0), 0)
        n = len(d.requests)
        f.add(card_title("Requests", None, f"{w.plural(n, 'request')}, in the order they ran. Open one to see what it asked for "
                         "and what QuickBooks did." if n else "none"))
        items = [request_item(q, self.app, self.opened, d.longest) for q in d.requests]
        widths = line_up([i.head for i in items])
        if n and not self.narrow:
            result, secs, records = widths
            f.add(table_head([("", px(16)), ("Time", px(64)), ("Request", 0), ("Result", result), ("Duration", px(112) + secs),
                              ("Records", records)]))
        for i in items:
            f.add(i)
        return f

    def build_changes(self, d: Any) -> Node | None:
        if not d.changes:
            return None
        f = card((0, 0, 0, 0), 0)
        f.add(card_title("Changes", len(d.changes), "saved or tried in this session"))
        items = [change_item(c, self.app, self.opened) for c in d.changes]
        line_up([i.head for i in items])
        for i in items:
            f.add(i)
        return f


# ---------------------------------------------------------------- Approvals (AP-1 to AP-6, release 2)


class ApprovalsPage(Page):
    name = "approvals"
    title = "Approvals"
    subtitle = "Changes an assistant has prepared. Nothing is saved until someone approves it."

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.notices = self.section()
        self.waiting = self.section()
        self.expired = self.section()
        self.answered = self.section()
        self.end()
        self.focus_token = ""

    def open(self, token: str = "") -> None:
        self.focus_token = token
        self.refresh()
        if token:
            self.layout_now()
            target = self.find(f"approval:{token}")
            if target is not None:
                self.scroll_to(target)

    def refresh(self) -> None:
        super().refresh()
        m, pick, e = self.app.model, self.app.pick, (self.app.epoch, self.narrow)
        waiting, expired, answered = m.approvals(pick)
        notes = tuple(self.app.notices)
        self.notices.show_built((e, notes), lambda: self.build_notices(list(notes)))
        busy = tuple(sorted(self.app.busy))
        self.waiting.show_built((e, tuple(approval_sig(a) for a in waiting), busy), lambda: self.build_waiting(waiting))
        self.expired.show_built((e, tuple(approval_sig(a) for a in expired), busy), lambda: self.build_expired(expired))
        self.answered.show_built((e, tuple((a.t, a.token, a.badge, a.how, a.undo) for a in answered[:12]), busy),
                                 lambda: self.build_answered(answered[:12]))

    def build_notices(self, notes: list[tuple[str, str, str, float]]) -> Node | None:
        if not notes:
            return None
        col = vbox(gap=10)
        for tone, title, text, t in notes:
            close = icon_button("x_plain", "Close", lambda n=(tone, title, text, t): self.app.drop_notice(n), name="Close this message")
            col.add(Banner(tone, {"ok": "check_circle", "bad": "x", "warn": "warning"}.get(tone, "info"), title, text, "", close))
        return col

    def build_waiting(self, waiting: list[Approval]) -> Node:
        col = vbox(gap=16)
        head = hbox(gap=10)
        head.add(label("Waiting for you", "subtitle"))
        if waiting:
            head.add(Bubble(len(waiting), "light"))
        head.spring()
        col.add(head)
        for a in waiting:
            col.add(self.approval_card(a))
        if not waiting:
            f = card((20, 18, 20, 18), 6)
            f.add(label("Nothing waits for you.", "body"))
            f.add(label("When an assistant prepares a change, it waits here until someone approves or rejects it.", "body",
                        "secondary", wrap=True))
            col.add(f)
        return col

    def approval_card(self, a: Approval) -> Node:
        f = card((0, 0, 0, 0), 0)
        f.key = f"approval:{a.token}"
        body = vbox((24, 20, 24, 20), 18)
        top = hbox(gap=16)
        top.add(IconTile(ENTITY_ICONS.get(str(a.d.get("entity")), "document"), "info", 40), align="top")
        col = vbox(gap=4)
        tl = hbox(gap=8)
        tl.add(label(a.title, "subtitle", wrap=True, select=True))
        tl.add(Badge("Waiting for you", "warn", "person"))
        if a.warnings:
            tl.add(Badge(w.plural(len(a.warnings), "warning"), "warn", "warning"))
        tl.spring()
        col.add(tl)
        asked = hbox(gap=6)
        asked.add(Avatar(a.who, 20))
        asked.add(elided(f"Asked by {a.who.name} at {w.clock(a.created)}" + (" ·" if a.session_title else ""), "body", "secondary"))
        asked.spring()
        col.add(asked)
        if a.session_title:
            col.add(link(a.session_title, "", lambda: self.app.open_session_by(a.who, str(a.d.get("session") or ""), a.created)),
                    align="left")
        top.add(col, 1)
        if a.total:
            amount = vbox(gap=0)
            amount.add(label(a.total, "amount", right=True, select=True))
            if a.currency:
                amount.add(label(a.currency, "caption", "secondary", right=True))
            top.add(amount, align="top")
        body.add(top)
        body.add(two_columns(self.narrow, self.what(a), self.checks(a)))
        f.add(body)
        f.add(self.answer_bar(a))
        return f

    def what(self, a: Approval) -> Node:
        col = vbox(gap=10)
        col.add(label("What will be saved", "strong"))
        if a.sentence:
            col.add(label(a.sentence.rstrip(".") + ".", "body", wrap=True, select=True))
        heads = a.columns
        if a.lines:
            ncol = len(heads)
            g = Grid(ncol, 16, 0)
            for i, h in enumerate(heads):
                g.put(label(h, "caption", "secondary", right=i == ncol - 1 and bool(h)), 0, i)
            row = 1
            for text, amount in a.lines:
                g.put(divider(), row, 0, ncol)
                row += 1
                cells = text.split(", ", 1) if ncol == 3 else [text]
                cells = (cells + [""])[: ncol - 1]
                for i, c in enumerate(cells):
                    g.put(padded(label(c, "body", wrap=True, select=True), (0, 9, 0, 9)), row, i)
                g.put(padded(label(amount, "body", select=True, right=True), (0, 9, 0, 9)), row, ncol - 1)
                row += 1
            if a.total:
                g.put(divider(), row, 0, ncol)
                row += 1
                g.put(padded(label("Total", "strong"), (0, 9, 0, 9)), row, 0)
                g.put(padded(label(a.total, "strong", right=True, select=True), (0, 9, 0, 9)), row, ncol - 1)
            for i in range(ncol - 1):
                g.grow_col(i)
            col.add(g)
        if a.reason:
            r = box("subtle", (14, 10, 14, 10), 2)
            r.add(label(f"Reason {a.who.short} gave" if not a.who.unknown else "Reason given", "caption", "secondary"))
            r.add(label(a.reason, "body", wrap=True, select=True))
            col.add(r)
        return col

    def checks(self, a: Approval) -> Node:
        col = vbox(gap=10)
        col.add(label("Warnings", "strong"))
        if not a.warnings:
            line = hbox(gap=8)
            line.add(Icon("check_circle", "ok"))
            line.add(label("None", "body"), 1)
            col.add(line)
        for text in a.warnings:
            head, _, rest = text.partition(": ")
            b = box("warnBox", (14, 12, 14, 12), 10, horizontal=True)
            b.add(Icon("warning", "warn"), align="top")
            inner = vbox(gap=2)
            inner.add(label(head if rest else "Warning", "strong", "warn"))
            inner.add(label(rest or head, "body", wrap=True, select=True))
            b.add(inner, 1)
            col.add(b)
        if a.checks:
            col.space(6)
            col.add(label("Checks passed", "strong"))
            for text in a.checks:
                line = hbox(gap=8)
                line.add(Icon("check", "ok"), align="top")
                line.add(label(text, "body", wrap=True), 1)
                col.add(line)
        if a.note:
            line = hbox(gap=8)
            line.add(Icon("warning", "warn"), align="top")
            line.add(label(a.note, "caption", "warn", wrap=True), 1)
            col.add(line)
        return col

    def answer_bar(self, a: Approval) -> Node:
        bar = box("subtleSquare", (24, 14, 24, 14), 12, horizontal=True)
        bar.add(Icon("clock", "warn"))
        bar.add(self.ticker.add(label("", "strong", "warn"), lambda: f"Expires in {w.span(max(0.0, a.expires - self.app.now()))}"
                                if a.expires > self.app.now() else "Expired"))
        bar.add(elided(f"at {w.clock(a.expires)}, 15 minutes after it was prepared", "body", "secondary"), 1)
        approving = self.app.busy_with(f"approve:{a.token}")
        rejecting = self.app.busy_with(f"reject:{a.token}")
        if approving or rejecting:
            bar.add(label("Saving…" if approving else "Rejecting…", "body", "secondary"))
        reject = button("Reject", tooltip="Nothing is saved; the assistant is told you said no", on_click=lambda: self.app.reject(a))
        reject.key = f"reject:{a.token}"
        reject.enabled = not (approving or rejecting)
        bar.add(reject)
        ok = button("Approve and save", "accent", tooltip="" if a.can_approve else a.why_not, on_click=lambda: self.app.approve(a))
        ok.key = f"approve:{a.token}"
        ok.enabled = a.can_approve and not (approving or rejecting)
        bar.add(ok)
        if a.can_approve:
            return bar
        col = vbox()
        col.add(bar)
        why = hbox((24, 0, 24, 14), 8)
        why.add(Icon("info", "secondary"), align="top")
        why.add(label(a.why_not, "caption", "secondary", wrap=True), 1)
        col.add(why)
        return col

    def build_expired(self, expired: list[Approval]) -> Node | None:
        if not expired:
            return None
        col = vbox(gap=12)
        col.add(label("Expired", "subtitle"))
        for a in expired:
            f = card((20, 16, 20, 16), 8, "cardAlt")
            top = hbox(gap=12)
            top.add(IconTile(ENTITY_ICONS.get(str(a.d.get("entity")), "document"), "neutral", 32), align="top")
            names = vbox(gap=2)
            names.add(label(a.title, "strong", "secondary", wrap=True))
            names.add(label(f"Expired at {w.clock(a.expires)}, not saved · from {a.who.name}", "body", "secondary", wrap=True))
            names.add(label(f"Ask {a.who.short} to prepare it again if it is still needed.", "caption", "secondary", wrap=True))
            top.add(names, 1)
            dismiss = button("Dismiss", on_click=lambda x=a: self.app.dismiss(x))
            dismiss.key = f"dismiss:{a.token}"
            top.add(dismiss)
            f.add(top)
            col.add(f)
        return col

    def build_answered(self, answered: list[Answered]) -> Node:
        f = card((0, 0, 0, 0), 0)
        f.add(card_title("Answered", None, "last 7 days", go("All changes", lambda: self.app.show("changes"))))
        now = self.app.now()
        for a in answered:
            r = Row("rowPlain", (20, 10, 20, 10), 12, clickable=False)
            t = r.add(fixed(label(f"{w.short_day(a.t, now)} {w.clock(a.t)}", "body", "secondary", wrap=True), px(96)))
            t.tip = w.exact(a.t)
            r.add(pair(a.title, a.who.name), 1)
            r.add(holder(Badge(*a.badge), px(170)))
            r.add(fixed(label(a.how, "body", "secondary", wrap=True), px(190)))
            cell = hbox(gap=0)
            cell.spring()
            if a.undo:
                u = button("Undo", "", "undo", tooltip="Prepares the reverse change, to approve here",
                           on_click=lambda token=a.token: self.app.undo_token(token))
                u.key = f"undo:{a.token}"
                u.enabled = not self.app.busy_with(f"undo:{a.token}")
                cell.add(u)
            r.add(fixed(cell, px(84)))
            f.add(r)
        if not answered:
            f.add(empty("No answers in the last 7 days."))
        return f


# ---------------------------------------------------------------- Changes (CH-1 to CH-4)


class ChangesPage(Page):
    name = "changes"
    title = "Changes"
    subtitle = "Every change saved or tried in QuickBooks, with the reason the assistant gave."
    KINDS = [("all", "All"), ("done", "Saved"), ("failed", "Not saved"), ("unknown", "Check it")]

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.kind, self.who, self.period = "all", ALL, 7
        self.opened: set[str] = set()
        self.met: set[str] = set()  # changes already shown once, so one the person closed stays closed
        self.controls = self.section()
        self.list = self.section()
        self.foot("From the write log on this PC. QuickBooks also lists these changes in its Audit Trail, under the QuickBooks user "
                  "the app logs in as.")
        self.end()

    def refresh(self) -> None:
        super().refresh()
        m, e = self.app.model, (self.app.epoch, self.narrow)
        rows = m.changes(self.app.pick, since_of(self.app, self.period))
        mine = [c for c in rows if self.who == ALL or c.who.key == self.who]
        counts = {k: sum(1 for c in mine if k == "all" or c.rec.e.get("outcome") == k) for k, _ in self.KINDS}
        people = sorted({c.who.key: c.who for c in rows}.values(), key=lambda x: x.name.casefold())
        self.controls.show_built((e, self.kind, self.who, self.period, tuple(counts.items()), tuple(p.key for p in people)),
                                 lambda: self.build_controls(counts, people))
        shown = [c for c in mine if self.kind == "all" or c.rec.e.get("outcome") == self.kind]
        for c in shown:  # Not saved and Check it open by default: what to do before trying again is the point (CH-4)
            key = f"change:{c.rec.src}:{c.rec.line}"
            if key not in self.met:
                self.met.add(key)
                if c.rec.e.get("outcome") in ("failed", "unknown"):
                    self.opened.add(key)
        self.list.show_built((e, tuple(change_sig(c) for c in shown), tuple(sorted(self.app.busy))), lambda: self.build_list(shown))

    def build_controls(self, counts: dict[str, int], people: list[Any]) -> Node:
        row = hbox(gap=12)
        row.add(Segments([(k, f"{text} {counts[k]}") for k, text in self.KINDS], self.kind, self.set_kind))
        who = Dropdown([(ALL, "All assistants", "")] + [(p.key, p.name, "") for p in people], self.who, on_pick=self.set_who,
                       label_of=lambda v: next((p.name for p in people if p.key == v), "All assistants"))
        who.key = "who"
        row.add(who)
        period = Dropdown([(d, t, "") for d, t in PERIODS], self.period, on_pick=self.set_period)
        period.key = "period"
        row.add(period)
        row.spring()
        return row

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.refresh()

    def set_who(self, key: str) -> None:
        self.who = key
        self.refresh()

    def set_period(self, days: int) -> None:
        self.period = days
        self.refresh()

    def build_list(self, shown: list[ChangeRow]) -> Node:
        f = card((0, 0, 0, 0), 0)
        now = self.app.now()
        if not shown:
            f.add(empty(f"No changes {period_words(self.period)}." if self.kind == "all" else "None of this kind.", (20, 16, 20, 18)))
            return f
        day, first = "", True
        items = []
        for c in shown:
            d = w.day(c.rec.t, now)
            if d != day:
                day = d
                f.add(group_head(d, first=first))
                first = False
            items.append(change_item(c, self.app, self.opened))
            f.add(items[-1])
        line_up([i.head for i in items])
        return f


# ---------------------------------------------------------------- Problems (PR-1 to PR-4)


class ProblemsPage(Page):
    name = "problems"
    title = "Problems"
    subtitle = "Requests that failed, were blocked or were refused, in plain words, with what to do."

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.open_only, self.period = False, 7
        self.opened: set[str] = set()
        self.new_ids: set[str] = set()
        self.controls = self.section()
        self.list = self.section()
        self.foot("A problem is new until someone opens this page. The same failure repeated by one assistant folds into one row.")
        self.end()

    def shown(self) -> None:
        """Opening the page shows which problems are new, then counts them as seen."""
        self.new_ids = {p.pid for p in self.app.model.new_problems()}
        self.opened |= {f"problem:{pid}" for pid in self.new_ids}
        self.refresh()
        self.app.model.mark_problems_seen()
        self.app.changed()

    def refresh(self) -> None:
        super().refresh()
        m, e = self.app.model, (self.app.epoch, self.narrow)
        rows = m.problems(self.app.pick, since_of(self.app, self.period))
        still = sum(1 for p in rows if p.open)
        self.controls.show_built((e, self.open_only, self.period, len(rows), still), lambda: self.build_controls(len(rows), still))
        shown = [p for p in rows if p.open or not self.open_only]
        self.list.show_built((e, tuple(problem_sig(p, p.new or p.pid in self.new_ids) for p in shown), tuple(sorted(self.new_ids))),
                             lambda: self.build_list(shown))

    def build_controls(self, total: int, still: int) -> Node:
        row = hbox(gap=12)
        row.add(Segments([(False, f"All {total}"), (True, f"Still open {still}")], self.open_only, self.set_open))
        period = Dropdown([(d, t, "") for d, t in PERIODS], self.period, on_pick=self.set_period)
        period.key = "period"
        row.add(period)
        row.spring()
        return row

    def set_open(self, only: bool) -> None:
        self.open_only = only
        self.refresh()

    def set_period(self, days: int) -> None:
        self.period = days
        self.refresh()

    def build_list(self, shown: list[ProblemRow]) -> Node:
        f = card((0, 0, 0, 0), 0)
        now = self.app.now()
        if not shown:
            f.add(empty("Nothing went wrong " + period_words(self.period) + "." if not self.open_only else "No problem is still open.",
                        (20, 16, 20, 18)))
            return f
        day, first = "", True
        badges = max([px(176)] + [Badge(*p.badge).pref_w() for p in shown])  # a status word is never cut
        for p in shown:
            d = w.day(p.last.t, now)
            if d != day:
                day = d
                f.add(group_head(d, first=first))
                first = False
            if p.pid in self.new_ids and not p.new:
                p.new = True
            f.add(problem_item(p, self.app, self.opened, badges))
        return f


# ---------------------------------------------------------------- Safety (SA-1 to SA-4)


class SafetyPage(Page):
    name = "safety"
    title = "Safety"
    subtitle = "How assistants may work in this company file from this PC. Shown here, changed elsewhere: each setting says how."
    picker_below = True

    def __init__(self, app: "Controller", parent: wx.Window):
        super().__init__(app, parent)
        self.warning = self.section()
        self.cards = self.section()
        self.lock = self.section()
        self.end()

    def refresh(self) -> None:
        super().refresh()
        m, e = self.app.model, (self.app.epoch, self.narrow)
        s = m.safety(self.app.pick)
        warn = s["warning"]
        self.warning.show_built((e, banner_sig(warn) if warn else None), lambda: banner_widget(warn, self.app) if warn else None)
        sig = (e, tuple(s["quickbooks"]), s["quickbooks_note"], tuple(s["changes"]), tuple(s["privacy"]), tuple(s["quick"]))
        self.cards.show_built(sig, lambda: self.build_cards(s))
        self.lock.show_built((e,), self.build_lock)

    def build_cards(self, s: dict) -> Node:
        cols = 1 if self.narrow else 2
        g = grid(cols, 16)
        qb = [(k, v + (f" · {n}" if n else ""), "", "") for k, v, n in s["quickbooks"]]
        cards = [self.settings_card("QuickBooks", qb or [("Company", "not read yet", "", "")], note=s["quickbooks_note"]),
                 self.settings_card("Changes", s["changes"], chip="This PC"),
                 self.settings_card("Privacy and records", s["privacy"]),
                 self.settings_card("Keeping QuickBooks quick", s["quick"])]
        for i, c in enumerate(cards):  # equal columns, whatever the cards' text would ask for
            g.put(c, i // cols, i % cols)
        for c in range(cols):
            g.grow_col(c)
        return g

    def settings_card(self, title: str, rows: list[tuple[str, str, str, str]], chip: str = "", note: str = "") -> Node:
        f = card((20, 18, 20, 18), 0)
        head = hbox(gap=8)
        head.add(label(title, "subtitle"))
        if chip:
            head.add(Badge(chip, "neutral"))
        head.spring()
        f.add(head)
        f.space(14)
        for name, value, code, tone in rows:
            f.add(divider())
            r = hbox((0, 10, 0, 10), 16)
            r.add(fixed(label(name, "body", "secondary", wrap=True), px(140)), align="top")
            col = vbox(gap=2)
            line = hbox(gap=6)
            mark = {"ok": ("check", "ok"), "muted": ("slash", "secondary"), "warn": ("warning", "warn"), "bad": ("warning", "bad")}.get(tone)
            if mark:
                line.add(Icon(*mark, 14), align="top")
            line.add(label(value, "body", wrap=True, select=True), 1)
            col.add(line)
            if code:
                col.add(label(code, "mono", "secondary", wrap=True, select=True))
            r.add(col, 1, align="top")
            f.add(r)
        if note:
            f.space(8)
            f.add(label(note, "caption", "secondary", wrap=True))
        return f

    def build_lock(self) -> Node:
        f = card((20, 18, 20, 20), 0)
        a = vbox(gap=8)
        a.add(label("Settings are not a lock", "subtitle"))
        a.add(label("They stop mistakes: an assistant asked a question does not change the books by accident. They do not stop "
                    "an assistant that can run any command, because it can set them in its own command line.", "body", wrap=True))
        a.add(label("What does hold: your AI app's approval prompt for every save, and the rights of the QuickBooks user the app "
                    "logs in as. The logs and this monitor show what happened, and when a session ran with other settings.",
                    "body", "secondary", wrap=True))
        b = vbox(gap=8)
        b.add(label("How to change a setting", "strong"))
        b.add(label("Settings are Windows environment variables. In PowerShell, for example:", "body", wrap=True))
        code = box("subtle", (12, 8, 12, 8), 0)
        code.add(label("setx QBD_MAX_AMOUNT 5000", "mono", select=True))
        b.add(code)
        b.add(label("Then restart the assistant's app. This page shows the new value straight away; each session keeps the "
                    "settings it started with.", "body", "secondary", wrap=True))
        f.add(two_columns(self.narrow, a, b, (18, 32)))
        return f


PAGES = [OverviewPage, LivePage, SessionsPage, SessionPage, ApprovalsPage, ChangesPage, ProblemsPage, SafetyPage]
