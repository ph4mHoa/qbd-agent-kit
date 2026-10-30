"""What the pages share: the page frame (a title, a subtitle, the company-file picker, the portal button, sections that
rebuild only when their data changed) and the pieces several pages show: a session row, the running request, a
waiting request, a finished request, a change, a problem and a request of a session."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import wx

from .. import words as w
from ..model import ALL, LINE_WAIT, SLOW, ChangeRow, LiveCmd, ProblemRow, RequestRow, SessionRow
from . import widgets as W
from .widgets import (Avatar, Badge, Bar, Bubble, Button, Dropdown, Grid, Icon, Node, Row, Run, Section, Text, Ticker, best, box,
                      button, card, divider, elided, fixed, hbox, label, link, pair, px, rich, vbox)

if TYPE_CHECKING:
    from .app import Controller

SLOW_NOTE = "Large reads slow QuickBooks for everyone."
PAGE_MARGINS = (34, 26, 32, 32)
NARROW_MARGINS = (16, 16, 16, 24)
WIDE = 1180          # the content's widest
NARROW = 760         # below this window width, rows wrap and tiles go two by two
PORTAL_TIP = "Portal: a small window that stays on top"


class Scroller(wx.ScrolledWindow):
    """A page's scrolling area. The wheel scrolls it wherever the pointer is on the page."""

    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=wx.VSCROLL | wx.BORDER_NONE)
        self.SetScrollRate(0, px(20))
        self.ShowScrollbars(wx.SHOW_SB_NEVER, wx.SHOW_SB_DEFAULT)
        self.SetBackgroundColour(W.c("content"))
        self.turned = 0
        self.Bind(wx.EVT_MOUSEWHEEL, self.wheel)

    def wheel(self, e: wx.MouseEvent) -> None:
        if e.GetWheelAxis() != wx.MOUSE_WHEEL_VERTICAL:
            return
        x, y = self.GetViewStart()
        if e.IsPageScroll():
            step = max(1, self.GetClientSize()[1] // px(20) - 1)
            self.Scroll(x, max(0, y - step * (1 if e.GetWheelRotation() > 0 else -1)))
            return
        self.turned += e.GetWheelRotation() * max(1, e.GetLinesPerAction())
        units = int(self.turned / max(1, e.GetWheelDelta()))
        self.turned -= units * e.GetWheelDelta()
        if units:
            self.Scroll(x, max(0, y - units))


class PageRoot(W.Root):
    """A page's column: as wide as the window up to the design's widest, as tall as its content, scrolled."""

    def layout(self) -> None:
        self.pending = False
        sw = self.host
        cw, ch = sw.GetClientSize()
        if cw <= 0 or ch <= 0:
            return
        pl, _, pr, _ = self.node.inner()._pads()
        width = min(cw, px(WIDE) + pl + pr)
        height = self.node.height(width)
        sw.Freeze()
        try:
            sw.SetVirtualSize(cw, height)
            x, y = sw.CalcScrolledPosition(0, 0)
            self.node.place(x, y, width, max(height, ch))
        finally:
            sw.Thaw()


class Page:
    """One screen: a scrolling column of sections under a header."""

    name = ""
    title = ""
    subtitle = ""
    uses_picker = True
    picker_below = False   # the picker under the subtitle (Safety), not at the header's right

    def __init__(self, app: "Controller", parent: wx.Window):
        self.app = app
        self.ticker = Ticker()
        self.scroll = Scroller(parent)
        self.col = box("page", PAGE_MARGINS, 22)
        self.head = hbox(gap=12)
        self.left = vbox(gap=4)
        self.title_label = label(self.title, "title")
        self.left.add(self.title_label)
        self.sub = label(self.subtitle, "body", "secondary", wrap=True)
        self.left.add(self.sub)
        self.head.add(self.left, 1, align="top")
        self.right = hbox(gap=8)
        self.head.add(self.right, align="bottom")
        self.col.add(self.head)
        self.sections: list[Section] = []
        self.picker_box = Section()
        if self.picker_below:
            self.col.add(self.picker_box, align="left")
        else:
            self.right.add(self.picker_box, align="top")
        self.portal = W.icon_button("portal", PORTAL_TIP, app.show_portal, size=36)
        self.portal.key = "portal"
        self.right.add(self.portal, align="top")
        self.root: PageRoot | None = None

    # -------------------------------------------------------- sections

    def section(self) -> Section:
        s = Section()
        self.sections.append(s)
        self.col.add(s)
        return s

    def foot(self, text: str) -> None:
        self.col.add(label(text, "caption", "secondary", wrap=True))

    def end(self) -> None:
        """The page is built: its windows are made."""
        self.root = PageRoot(self.scroll, self.col)
        self.scroll.Bind(wx.EVT_SIZE, self.sized)

    def sized(self, e: wx.SizeEvent) -> None:
        if self.root is not None:
            self.root.layout()
        e.Skip()

    def refresh(self) -> None:
        """Shows the model's current state; built again only where the data changed. Narrow, the title, the picker
        and the portal button are in the window's bar instead."""
        narrow = self.narrow
        self.col.inner().pad = NARROW_MARGINS if narrow else PAGE_MARGINS
        self.title_label.show(bool(self.title) and not narrow)
        self.portal.show(not narrow)
        self.picker_box.show_built((self.app.pick, tuple((f.key, f.name, f.detail) for f in self.app.model.files()), narrow),
                                   self.picker if self.uses_picker and not narrow else lambda: None)
        if self.root is not None:
            self.root.schedule()

    def layout_now(self) -> None:
        if self.root is not None:
            self.root.layout()

    def tick(self) -> None:
        self.ticker.tick()

    def shown(self) -> None:
        """The page became the visible one."""

    @property
    def narrow(self) -> bool:
        return self.app.narrow

    def to_top(self) -> None:
        self.scroll.Scroll(0, 0)

    def scroll_to(self, node: W.Win, margin: int = 40) -> None:
        """Scrolls so node shows whole, or its top when it is taller than the page."""
        if not node.win:
            return
        sw = self.scroll
        top = sw.CalcUnscrolledPosition(sw.ScreenToClient(node.win.GetScreenPosition()))[1]
        height = node.win.GetSize()[1]
        view_top = sw.GetViewStart()[1] * px(20)
        view_h = sw.GetClientSize()[1]
        if top - px(margin) < view_top or top + height + px(margin) > view_top + view_h:
            want = top - px(margin) if height + 2 * px(margin) > view_h or top < view_top else top + height + px(margin) - view_h
            sw.Scroll(0, max(0, want // px(20)))

    def find(self, key: str) -> W.Win | None:
        return next((n for n in self.col.walk() if isinstance(n, W.Win) and n.key == key), None)

    # -------------------------------------------------------- the company-file picker (GL-4)

    def picker(self) -> Node | None:
        files = self.app.model.files()
        footer = [(ALL, "All company files")]
        if len(files) <= 1 and self.app.pick in (ALL, files[0].key if files else ALL):
            if not files:
                return None
            d = Dropdown([(f.key, f.name, f.detail) for f in files], files[0].key, icon="file", footer=footer, on_pick=self.app.set_pick)
        else:
            d = Dropdown([(f.key, f.name, f.detail) for f in files], self.app.pick, icon="file", footer=footer, on_pick=self.app.set_pick,
                         label_of=lambda v: next((f.name for f in files if f.key == v), "All company files"))
        d.key = "picker"
        d.tip = "The company file every count and list follows"
        d.max_w = px(320)
        return d


# ---------------------------------------------------------------- pieces


def holder(node: Node, width: int = 0) -> Node:
    """A cell of a list row with node on its left, at least width pixels wide, so badges line up in a column."""
    h = hbox(gap=0)
    h.add(node)
    h.spring()
    h.fixed_w = max(width, best(node))
    return h


def fits(t: Text, width: int) -> int:
    """A number column's width in pixels: the design's, or more when the text needs it ("under 0.1 s", "12,480
    records"), so a right-aligned value is never cut on the left."""
    return max(px(width), t.pref_w())


def line_up(rows: list[Row]) -> list[int]:
    """One width for each column on every row of a list, the widest a row needs, so the columns still line up when a
    row's text ("under 0.1 s", "Previewed, not saved") made its cell wider than the design's; the widths are returned
    for the column names over the list."""
    widths = [max(best(c) for c in column) for column in zip(*(r.cells for r in rows))]
    for r in rows:
        for c, width in zip(r.cells, widths):
            c.fixed_w = width
    return widths


def who_line(who: Any, after: str = "", size: int = 24, strong: bool = True) -> tuple[Node, Text | None]:
    """Avatar, name, and muted words after it: "Claude (Claude Code) · session started 2 min ago"; the words after it
    are returned too, for a time that follows the clock."""
    row = hbox(gap=8)
    row.add(Avatar(who, size))
    row.add(label(who.name if not who.unknown else "Unknown assistant", "strong" if strong else "body"))
    if who.unknown and who.user:
        row.add(label(f"· {who.user}", "body", "secondary"))
    if after:
        return row, row.add(elided(after, "body", "secondary"), 1)
    row.spring()
    return row, None


def session_row(s: SessionRow, app: "Controller", columns: bool = False) -> Row:
    """One session on one line: assistant, what it was asked, time, counts and result; it opens the session."""
    r = Row(pad=(20, 10, 20, 10), gap=12, on_click=lambda sid=s.sid: app.open_session(sid))
    r.key = f"session:{s.sid}"
    r.min_h = 44 if columns else 48
    r.name = f"{s.who.name}: {s.asked}. {s.time_text()}. {s.counts_text()}. {s.badge[0]}"
    who = s.who.name if not s.who.unknown else "Unknown assistant"
    if app.narrow:  # what it was asked first, then who, when and how much (the Narrow board)
        r.add(Avatar(s.who), align="top")
        top = vbox(gap=2)
        top.add(elided(s.asked, "strong"))
        top.add(label(f"{who} · {s.time_text()} · {s.counts_text()}", "caption", "secondary", wrap=True))
        r.add(top, 1)
        r.add(Badge(*s.badge), align="top")
        return r
    r.add(Avatar(s.who))
    name = r.add(elided(who, "strong"))
    if columns:
        fixed(name, px(130))
    asked = r.add(elided(s.asked, "body", "secondary"), 1)
    asked.tip = s.asked
    if columns:
        asked.least_w = px(180)
    t = r.add(label(s.time_text(), "body", "secondary"))
    t.tip = w.exact(s.start)
    if columns:
        fixed(t, px(96))
    counts = r.add(elided(s.counts_text(), "body", "secondary"))
    fixed(counts, px(236) if columns else min(px(260), counts.pref_w()))
    r.cells = [r.add(holder(Badge(*s.badge), px(112) if columns else 0))]
    return r


def stop_panel(cmd: LiveCmd, app: "Controller", compact: bool = False, side: bool = False) -> Node:
    """Stop after this step (release 2), with what it does: the note under the button in a grey box, beside it (side),
    or left out (compact). The button is hidden where there is no step to stop after."""
    plain = compact or side
    f = box("plain" if plain else "subtle", (0, 0, 0, 0) if plain else (12, 12, 12, 12), 8)
    asked = cmd.stop_asked
    b = Button("Stopping after this step…" if asked else "Stop after this step", "quiet" if plain else "", "stop_square", cmd.stop_note,
               lambda: app.stop(cmd), height=32 if compact else 36, pad=12 if compact else 14)
    b.key = f"stop:{cmd.pid}"
    b.enabled = not asked and cmd.stoppable
    b.hidden = not cmd.stoppable
    if side:
        row = hbox(gap=12)
        row.add(b)
        row.add(label(cmd.stop_note, "caption", "secondary", wrap=True), 1)
        f.add(row)
    else:
        f.add(b, align="left")
        if not compact:
            f.add(label(cmd.stop_note, "caption", "secondary", wrap=True))
    return f


def running_card_body(cmd: LiveCmd, app: "Controller", ticker: Ticker, big: bool) -> Node:
    """The request talking to QuickBooks now: who, what, the current step and its progress (LV-1 to LV-3, OV-2)."""
    col = vbox(gap=16 if big else 14)
    now = app.now
    if big:
        line = hbox(gap=10)
        line.add(Avatar(cmd.who, 32))
        names = vbox(gap=0)
        names.add(label(cmd.who.name, "strong"))
        if cmd.session_title:
            names.add(link(cmd.session_title, "", lambda: app.open_session_of(cmd), role="caption"), align="left")
        line.add(names, 1)
        col.add(line)
    else:
        line, after = who_line(cmd.who, " ")
        began = app.model.session_start(cmd)
        ticker.add(after, lambda: f"· session started {w.ago(began, now())}")  # type: ignore[arg-type]
        col.add(line)
    what = col.add(vbox(gap=0))
    what.add(elided(cmd.title, "lead_strong"))
    if big:
        defaults = ", ".join(f"{a.lower()} {v}" for a, v in w.asked_for(cmd.info.get("command"), cmd.info.get("args"), app.model.tools)[:3])
        if cmd.args and cmd.args != "defaults":
            what.add(label(cmd.args, "body", "secondary", wrap=True))
        elif defaults:
            what.add(label(f"Defaults: {defaults}".replace(" (default)", ""), "body", "secondary", wrap=True))
    elif cmd.step:
        doing = f"Reading {cmd.step[0].lower() + cmd.step[1:]}" if not cmd.report else cmd.doing.rstrip("…")
        if cmd.page and cmd.pages:
            doing += f" · page {cmd.page} of {cmd.pages}"
        what.add(elided(doing, "body", "secondary"))
    known = cmd.percent is not None
    bar = Bar(cmd.percent / 100 if known else None, "accent", 6, name="Progress")
    if big:
        progress = col.add(vbox(gap=8))
        head = hbox(gap=12)
        if known:
            head.add(elided(f"{cmd.step}, page {cmd.page} of {cmd.pages}" if cmd.pages else cmd.step, "body"), 1)
            head.add(label(f"{cmd.percent}%", "strong"))
        else:
            head.add(elided(cmd.doing or "Waiting for QuickBooks to answer…", "body"), 1)
            head.add(ticker.add(label("", "strong"), lambda: w.span(cmd.elapsed(now()))))
        progress.add(head)
        progress.add(bar)
        if known:
            total = f"{w.num(cmd.read)} of {w.num(cmd.total)} records read"
            if cmd.capped:
                total += f" · this read stops at {w.num(cmd.cap)}"
            progress.add(label(total, "body", "secondary", wrap=True))
        else:
            progress.add(label("QuickBooks doesn't say how far along a report is, so there is no percentage." if cmd.report else
                               "QuickBooks says how many records are left after the first page; until then there is no percentage.",
                               "body", "secondary", wrap=True))
        slow = box("warnBox", (14, 10, 14, 10), 8, horizontal=True)
        slow.add(Icon("clock", "warn"), align="top")
        slow.add(rich([Run("Slow", "strong", "warn"), Run(f" · {SLOW_NOTE}", "body")]), 1)
        col.add(slow)
        ticker.every(slow, lambda: slow.show(cmd.elapsed(now()) >= SLOW))
        steps = col.add(box("rowPlain", (0, 12, 0, 0), 10))
        for name, text in cmd.done:
            line = hbox(gap=10)
            line.add(Icon("check_circle", "ok"))
            line.add(elided(name, "body"), 1)
            line.add(label(text, "body", "secondary"))
            steps.add(line)
        if cmd.step:
            line = hbox(gap=10)
            line.add(Icon("running", "accent"))
            line.add(elided(cmd.step, "strong"), 1)
            if known and cmd.pages:
                line.add(label(f"reading · page {cmd.page} of {cmd.pages}", "strong", "accent"))
            else:
                verb = "building" if cmd.report else "reading" if cmd.page else "waiting"
                line.add(ticker.add(label("", "strong", "accent"), lambda: f"{verb} · {w.span(app.now() - (cmd.step_since or cmd.since))}"))
            steps.add(line)
        steps.hidden = not steps.inner().items
        col.add(stop_panel(cmd, app))
        return col
    col.add(bar)
    foot = hbox(gap=12)
    if known:
        total = f"{w.num(cmd.read)} of {w.num(cmd.total)} records" + (f" · this read stops at {w.num(cmd.cap)}" if cmd.capped else "")
        foot.add(elided(total, "body"), 1)
        foot.add(ticker.add(label("", "body", "secondary"), lambda: f"{cmd.percent}% · {w.span(cmd.elapsed(now()))}"))
    else:
        foot.add(elided(cmd.doing or "Waiting for QuickBooks…", "body"), 1)
        foot.add(ticker.add(label("", "body", "secondary"), lambda: w.span(cmd.elapsed(now()))))
    col.add(foot)
    col.add(stop_panel(cmd, app, compact=app.narrow, side=not app.narrow))
    return col


def waiting_row(cmd: LiveCmd, app: "Controller", ticker: Ticker, boxed: bool) -> Node:
    """A command waiting for its turn, with how long it has waited of its 60 s (LV-4)."""
    what = cmd.title + (f" · {cmd.args}" if cmd.args and cmd.args != "defaults" else "")
    if boxed:
        f = card((14, 12, 14, 12), 8)
        top = hbox(gap=10)
        top.add(Avatar(cmd.who), align="top")
        names = vbox(gap=0)
        names.add(elided(cmd.who.name, "strong"))
        names.add(elided(what, "body", "secondary"))
        top.add(names, 1)
        top.add(Badge("In line", "warn", "clock"), align="top")
        f.add(top)
        foot = hbox(gap=10)
        bar = foot.add(Bar(min(1.0, cmd.elapsed(app.now()) / LINE_WAIT), "warn", 4, name="Time waited"), 1)
        ticker.every(bar, lambda: bar.set(min(1.0, cmd.elapsed(app.now()) / LINE_WAIT)))
        foot.add(ticker.add(label("", "caption", "secondary"), lambda: f"waited {w.span(cmd.elapsed(app.now()))} of {LINE_WAIT} s"))
        f.add(foot)
        return f
    row = hbox(gap=8)
    row.add(Avatar(cmd.who))
    row.add(label(cmd.who.name, "strong"))
    row.add(elided(what, "body", "secondary"), 1)
    badge = Badge("In line", "warn", "clock")
    ticker.every(badge, lambda: badge.set_word(f"In line {w.span(cmd.elapsed(app.now()))}"))
    row.add(badge)
    return row


def finished_row(r: Any, app: "Controller") -> Row:
    """A request that just ended: time, assistant, title, result, duration and records."""
    who = app.model.assistants.of(r.e)
    row = Row(pad=(20, 10, 20, 10), gap=12, on_click=lambda: app.open_call(r))
    row.key = f"call:{r.src}:{r.line}"
    row.min_h = 48
    t = row.add(label(w.clock(r.t, seconds=True), "body", "secondary"))
    t.tip = w.exact(r.t)
    row.add(Avatar(who))
    args = w.args_line(r.e)
    title = str(r.e.get("title") or r.e.get("command"))
    row.add(pair(title, " · ".join(b for b in [args if args != "defaults" else "", who.name] if b)), 1)
    badge = w.call_badge(r.e)
    cell = row.add(holder(Badge(*badge), px(184)))
    d = row.add(label(w.secs(r.e.get("ms")), "body", "secondary", right=True))
    fixed(d, fits(d, 56))
    n = row.add(label(w.plural(int(r.e.get("items") or 0), "record"), "body", "secondary", right=True))
    fixed(n, fits(n, 96))
    row.cells = [cell, d, n]
    row.name = f"{w.clock(r.t)} {title}, {badge[0]}, {d.text()}, {n.text()}"
    return row


def kv_grid(rows: list[tuple[str, str]], mono: bool = False, label_width: int = 150, value_tone: str = "") -> Grid:
    g = Grid(2, 16, 6)
    g.grow_col(1)
    for i, (k, v) in enumerate(rows):
        a = label(k, "caption" if mono else "body", "secondary")
        a.least_w = px(min(label_width, 120))
        g.put(a, i, 0)
        g.put(label(v, "mono" if mono else "body", value_tone, wrap=True, select=True), i, 1)
    return g


class Expander(W.Surface):
    """A row that opens to show more, one level at a time; it remembers being open across rebuilds."""

    def __init__(self, key: str, opened: set[str], header: Callable[[Row], None], body: Callable[[], Node], look: str = "row",
                 pad: tuple = (20, 12, 20, 12), indent: int = 56):
        super().__init__("plain")
        self.opened, self.body_fn, self.indent = opened, body, indent
        self.open_key = key
        self.head = Row(look, pad, 12, on_click=self.toggle)
        self.head.key = key
        self.chev = Icon("chevron_down" if key in opened else "chevron_right", "secondary")
        self.head.add(self.chev)
        header(self.head)
        self.add(self.head)
        self.body: Node | None = None
        self.head.description = "expanded" if key in opened else "collapsed"
        if key in opened:
            self._open()

    def _open(self) -> None:
        holder_ = vbox((self.indent, 0, 20, 16), 0)
        holder_.add(self.body_fn())
        self.body = self.add(holder_)
        if self.win:
            holder_.realize(self.win)

    def toggle(self) -> None:
        if self.open_key in self.opened:
            self.opened.discard(self.open_key)
            if self.body is not None:
                self.body.kill()
                self.inner().remove(self.body)
                self.body = None
            self.chev.set_icon("chevron_right")
        else:
            self.opened.add(self.open_key)
            self._open()
            self.chev.set_icon("chevron_down")
        self.head.description = "expanded" if self.open_key in self.opened else "collapsed"
        self.request_layout()


def code_block(text: str, name: str) -> Node:
    """Text as a program wrote it, in a monospaced box, with a button that copies it."""
    top = hbox(gap=0)
    code = box("code", (12, 8, 12, 8))
    t = code.add(Text(text, "mono", wrap=True, select=True))
    t.name = name
    top.add(code, 1, align="fill")
    top.add(W.icon_button("copy", "Copy", lambda: W.copy_text(text), name=f"Copy the {name.lower()}"), align="top")
    return top


def technical(text: str, key: str, opened: set[str], title: str = "Technical details", sub: str = "the call-log entry, as written") -> Node:
    """The entry as qbd wrote it, one click deep, for the IT person (SE-6)."""
    frame = box("expanderBox")

    def head(h: Row) -> None:
        h.add(pair(title, sub), 1)

    frame.add(Expander(key, opened, head, lambda: code_block(text, title), look="rowFlat", pad=(14, 10, 14, 10), indent=0))
    return frame


def tech_grid(rows: list[tuple[str, str]], key: str, opened: set[str]) -> Node:
    frame = box("expanderBox")

    def head(h: Row) -> None:
        h.add(label("Technical details", "body"), 1)

    def body() -> Node:
        b = vbox((20, 4, 16, 12), 0)
        b.add(kv_grid(rows, mono=True))
        return b

    frame.add(Expander(key, opened, head, body, look="rowFlat", pad=(14, 10, 14, 10), indent=0))
    return frame


def steps_list(steps: list[str]) -> Node:
    col = vbox(gap=4)
    for i, s in enumerate(steps, 1):
        line = hbox(gap=6)
        line.add(fixed(label(f"{i}.", "body"), px(16)), align="top")
        line.add(label(s, "body", wrap=True), 1)
        col.add(line)
    return col


def problem_body(p: ProblemRow, opened: set[str]) -> Node:
    col = vbox(gap=10)
    col.add(label(p.text, "lead", wrap=True, select=True))
    col.space(6)
    col.add(label("What to do", "strong"))
    col.add(steps_list(p.steps))
    if p.solved:
        line = hbox(gap=8)
        line.add(Icon("check_circle", "ok"), align="top")
        line.add(label(p.solved, "body", "ok", wrap=True), 1)
        col.add(line)
    col.add(tech_grid(p.technical, f"tech:{p.pid}", opened))
    return col


def problem_item(p: ProblemRow, app: "Controller", opened: set[str], badge_width: int = 0) -> Expander:
    def head(h: Row) -> None:
        t = h.add(fixed(label(w.clock(p.last.t), "body", "secondary"), px(44)))
        t.tip = w.exact(p.last.t)
        h.add(Avatar(p.who))
        if p.new:
            h.add(Badge("New", "new"))
        h.add(pair(p.title, " · ".join(b for b in [p.args, p.who.name] if b)), 1)
        h.add(holder(Badge(*p.badge), badge_width or px(176)))
        st = hbox(gap=6)
        if p.status[1] == "ok":
            st.add(Icon("check", "ok"), align="top")
        st.add(label(p.status[0], "body", {"ok": "ok", "warn": "warn"}.get(p.status[1], "secondary"), wrap=True), 1)
        h.add(fixed(st, px(150)))

    ex = Expander(f"problem:{p.pid}", opened, head, lambda: problem_body(p, opened), indent=76)
    ex.head.name = f"{p.title}, {p.badge[0]}, {p.status[0]}"
    return ex


def diff_cell(text: str, look: str) -> Node:
    b = box(look, (8, 2, 8, 2))
    b.add(label(text or "(empty)", "body", "secondary" if look == "diffBefore" else "", wrap=True, select=True))
    return b


def change_body(c: ChangeRow, app: "Controller") -> Node:
    col = vbox(gap=12)
    top = hbox(gap=32)
    if c.reason:
        r = vbox(gap=2)
        r.add(label(f"Reason {c.who.short} gave" if not c.who.unknown else "Reason given", "caption", "secondary"))
        r.add(label(c.reason, "body", wrap=True, select=True))
        top.add(r, align="top")
    if c.saved:
        s = vbox(gap=2)
        s.add(label("Saved", "caption", "secondary"))
        s.add(label(c.saved, "body", wrap=True, select=True))
        top.add(s, align="top")
    top.spring()
    if c.reason or c.saved:
        col.add(top)
    if c.diff:
        g = Grid(3, 24, 8)
        for i, h in enumerate(("Field", "Before", "After")):
            g.put(label(h, "caption", "secondary"), 0, i)
        g.put(divider(), 1, 0, 3)
        for r_, (field, before, after) in enumerate(c.diff, 2):
            g.put(label(field, "body"), r_, 0)
            g.put(diff_cell(before, "diffBefore"), r_, 1, align="left")
            g.put(diff_cell(after, "diffAfter"), r_, 2, align="left")
        g.grow_col(1)
        g.grow_col(2)
        col.add(g)
    if c.explain:
        col.add(label(c.explain, "body", wrap=True))
        if c.todo:
            col.add(label(c.todo, "body", "secondary", wrap=True))
    if c.undo_text:
        line = hbox(gap=12)
        if c.undo:
            b = button("Undo", "", "undo", on_click=lambda: app.undo(c))
            b.key = f"undo:{c.rec.e.get('token')}"
            line.add(b, align="top")
            line.add(label(c.undo_text, "caption", "secondary", wrap=True), 1)
        else:
            line.add(Icon("info", "secondary"), align="top")
            line.add(label(c.undo_text, "body", "secondary", wrap=True), 1)
            if "PC" in c.undo_text:
                line.add(link("Safety settings", "", lambda: app.show("safety")), align="top")
        col.add(line)
    return col


def change_item(c: ChangeRow, app: "Controller", opened: set[str]) -> Expander:
    cells: list[Node] = []

    def head(h: Row) -> None:
        t = h.add(fixed(label(w.clock(c.rec.t), "body", "secondary"), px(44)))
        t.tip = w.exact(c.rec.t)
        h.add(Avatar(c.who))
        h.add(pair(c.title, " · ".join(b for b in [c.about, c.who.name] if b)), 1)
        amt = h.add(label(c.amount, "body", right=True))
        fixed(amt, fits(amt, 90))
        cells.extend([amt, h.add(holder(Badge(*c.badge), px(110)))])

    ex = Expander(f"change:{c.rec.src}:{c.rec.line}", opened, head, lambda: change_body(c, app), indent=76)
    ex.head.name = f"{c.title}, {c.badge[0]}"
    ex.head.cells = cells
    return ex


def request_body(q: RequestRow, app: "Controller", opened: set[str]) -> Node:
    col = vbox(gap=10)
    two = hbox(gap=16)
    a = card((16, 14, 16, 14), 8)
    a.add(label("What it asked for", "strong"))
    if q.defaults_only:
        a.add(label("No arguments given, so the defaults applied." if q.asked else "No arguments given.", "body", "secondary", wrap=True))
    if q.asked:
        a.add(kv_grid(q.asked, label_width=150))
    two.add(a, 1, align="fill")
    s = card((16, 14, 16, 14), 8)
    s.add(label("QuickBooks steps", "strong"))
    if not q.steps:
        s.add(label("It did not reach QuickBooks.", "body", "secondary"))
    for name, text in q.steps:
        line = hbox(gap=8)
        line.add(elided(name, "body"), 1)
        line.add(label(text, "body", "secondary"))
        s.add(line)
    two.add(s, 1, align="fill")
    col.add(two)
    if q.problem:
        col.add(label(q.problem["text"], "body", wrap=True))
        col.add(label("What to do", "strong"))
        col.add(steps_list(q.problem["steps"]))
    elif q.records:
        col.add(label(f"QuickBooks sent back {w.plural(q.records, 'record')}. They are counted, not copied: no names, amounts or "
                      "other values from its answers are kept.", "caption", "secondary", wrap=True))
    col.add(technical(q.technical, f"tech:{q.rec.src}:{q.rec.line}", opened))
    return col


def request_item(q: RequestRow, app: "Controller", opened: set[str], longest: int) -> Expander:
    cells: list[Node] = []

    def head(h: Row) -> None:
        t = h.add(fixed(label(w.clock(q.rec.t, seconds=True), "body", "secondary"), px(64)))
        t.tip = w.exact(q.rec.t)
        h.add(pair(q.title, q.args), 1)
        cell = h.add(holder(Badge(*q.badge), px(150)))
        if not app.narrow:
            h.add(fixed(Bar(q.ms / longest if longest else 0, "neutral", 4, name="Duration"), px(100)))
        d = h.add(label(w.secs(q.ms), "body", "secondary", right=True))
        fixed(d, fits(d, 52))
        n = h.add(label(w.plural(q.records, "record"), "body", "secondary", right=True))
        fixed(n, fits(n, 88))
        cells.extend([cell, d, n])

    ex = Expander(f"request:{q.rec.src}:{q.rec.line}", opened, head, lambda: request_body(q, app, opened), indent=56)
    ex.head.name = f"{w.clock(q.rec.t)} {q.title}, {q.badge[0]}, {w.secs(q.ms)}, {w.plural(q.records, 'record')}"
    ex.head.cells = cells
    return ex


def group_head(text: str, more: str = "", first: bool = False) -> Node:
    """A day's heading in a list; the first one has no line above it, at the card's top."""
    f = box("groupHeadTop" if first else "groupHead", (20, 10, 20, 10), 8, horizontal=True)
    f.add(label(text, "strong"))
    if more:
        f.add(label(more, "body", "secondary"))
    f.spring()
    return f


def card_head(title: str, count: int | None = None, more: str = "", action: Node | None = None) -> Node:
    row = hbox(gap=10)
    row.add(label(title, "subtitle"))
    if count is not None:
        row.add(Bubble(count, "light"))
    if more:
        row.add(elided(more, "body", "secondary"), 1)
    else:
        row.spring()
    if action is not None:
        row.add(action)
    return row
