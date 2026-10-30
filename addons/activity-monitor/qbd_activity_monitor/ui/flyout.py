"""The tray's panel (TR-2 to TR-5): what runs now, what waits, today's numbers, and the way into the window. It opens
above the tray icon and closes when it loses focus or on Esc."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import wx

from .. import words as w
from ..model import LiveCmd
from . import widgets as W
from .base import stop_panel
from .pages import live_sig
from .widgets import Avatar, Badge, Bar, Button, Icon, IconTile, Image, Node, Row, Section, Ticker, box, card, elided, hbox, label, px, vbox

if TYPE_CHECKING:
    from .app import Controller

SIZE = (360, 520)


class Flyout(wx.Frame):
    def __init__(self, app: "Controller", logo: wx.Image):
        super().__init__(None, title="Activity Monitor", style=wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.app = app
        self.ticker = Ticker()
        self.closed_at = 0.0
        self.SetClientSize(px(SIZE[0]), px(SIZE[1]))
        top = box("flyout")
        head = box("plain", (16, 16, 16, 12), 12, horizontal=True)
        head.add(Image(logo, 28))
        names = vbox(gap=0)
        names.add(label("Activity Monitor", "strong"))
        self.company = names.add(elided("", "caption", "secondary"))
        head.add(names, 1)
        self.badge = head.add(Section())
        top.add(head)
        self.running = top.add(Section())
        self.lines = top.add(Section())
        top.spring()
        foot = box("flyoutFoot", (16, 12, 16, 12), 12, horizontal=True)
        self.footer = foot.add(label("", "caption", "secondary", wrap=True), 1)
        foot.add(Button("Open activity", "accent", on_click=lambda: self.go("overview"), height=32))
        top.add(foot)
        self.root = W.Root(self, top)
        self.hook = W.on_char_hook(self)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)
        self.Bind(wx.EVT_SIZE, lambda e: self.root.layout())
        self.retheme()

    def refresh(self) -> None:
        t = self.app.model.tray()
        e = self.app.epoch
        self.company.set_text(self.app.model.company_name(self.app.model.default_file()) or "No company file yet")
        self.badge.show_built((e, t.badge), lambda: Badge(*t.badge, height=24) if t.badge else None)
        self.running.show_built((e, live_sig(t.running), tuple(self.app.busy)), lambda: self.build_running(t.running))
        self.lines.show_built((e, tuple(t.lines)), lambda: self.build_lines(t.lines))
        self.footer.set_text(t.footer)

    def build_running(self, c: LiveCmd | None) -> Node | None:
        if c is None:
            return None
        holder = vbox((12, 0, 12, 0))
        f = holder.add(card((14, 12, 14, 12), 8))
        top = hbox(gap=8)
        top.add(Badge("Running", "info", "running"))
        top.spring()
        top.add(self.ticker.add(label("", "caption", "secondary"), lambda: w.span(c.elapsed(self.app.now()))))
        f.add(top)
        line = hbox(gap=10)
        line.add(Avatar(c.who))
        names = vbox(gap=0)
        names.add(elided(c.title, "strong"))
        step = (f"{c.step}, page {c.page}" + (f" of {c.pages}" if c.pages else "")) if c.page else (c.doing or "waiting for QuickBooks")
        names.add(elided(f"{c.who.name} · {step}", "caption", "secondary"))
        line.add(names, 1)
        f.add(line)
        known = c.percent is not None
        f.add(Bar(c.percent / 100 if known else None, "accent", 4, name="Progress"))
        if known:
            nums = hbox(gap=8)
            nums.add(label(f"{w.num(c.read)} of {w.num(c.total)} records", "caption"), 1)
            nums.add(label(f"{c.percent}%", "caption", "secondary"))
            f.add(nums)
        f.add(stop_panel(c, self.app, compact=True))  # type: ignore[arg-type]
        return holder

    def build_lines(self, lines: list[tuple[str, str, str, str, str]]) -> Node:
        if not lines:
            empty = vbox((16, 12, 16, 0))
            empty.add(label("Nothing waits for you.", "body", "secondary"))
            return empty
        col = vbox((6, 8, 6, 0), 2)
        for icon_name, tone, title, detail, page in lines:
            r = Row("trayLine", (10, 6, 10, 6), 12, on_click=lambda p=page: self.go(p))
            r.key = f"line:{page}:{icon_name}"
            r.name = f"{title}. {detail}"
            r.min_h = 60
            r.add(IconTile(icon_name, tone, 28))
            names = vbox(gap=0)
            names.add(label(title, "strong"))
            names.add(elided(detail, "caption", "secondary"))
            r.add(names, 1)
            r.add(Icon("chevron_right", "secondary"))
            col.add(r)
        return col

    def go(self, page: str) -> None:
        self.Hide()
        self.app.show(page)

    def show_near(self, at: wx.Point) -> None:
        """Opens above (or beside) the tray icon, where the pointer is, inside the screen's free area."""
        self.refresh()
        i = wx.Display.GetFromPoint(at)
        a = wx.Display(i if i != wx.NOT_FOUND else 0).GetClientArea()
        width, height = self.GetSize()
        m = px(8)
        x = min(max(a.x + m, at.x - width // 2), a.x + a.width - width - m)
        y = min(max(a.y + m, at.y - height - m), a.y + a.height - height - m)
        self.Move(x, y)
        self.Show()
        self.Raise()
        self.root.layout()

    def just_closed(self) -> bool:
        """Clicking the tray icon while the panel is open first takes its focus away, which closes it: that click
        should not open it again."""
        return time.time() - self.closed_at < 0.4

    def on_activate(self, e: wx.ActivateEvent) -> None:
        if not e.GetActive() and self.IsShown():
            self.closed_at = time.time()
            self.Hide()
        e.Skip()

    def on_key(self, e: wx.KeyEvent) -> None:
        if e.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
            return
        self.hook(e)

    def tick(self) -> None:
        if self.IsShown():
            self.ticker.tick()

    def retheme(self) -> None:
        self.SetBackgroundColour(W.c("mica"))
        W.refresh_all(self)

