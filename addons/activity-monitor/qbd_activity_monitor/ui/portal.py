"""The portal (the Portal board): a small window that stays on top in a corner of the screen, so whoever sits at the PC
sees what an assistant does in QuickBooks while they work. It shows one state, the most pressing; Expand opens the
window on the page that fits and the portal closes. Dragged by its top bar it settles in the nearest corner, 12 px
inside the screen's free area, and it keeps its corner on each screen; with the focus on its grip, the arrow keys move
it from corner to corner. It stays on screen while people work, so like a notification it shows who, the request's
title, counts and times, never an amount or a name from the books."""
from __future__ import annotations

import math
import sys
from typing import TYPE_CHECKING, Callable

import wx

from .. import words as w
from ..model import LINE_WAIT, SLOW, Portal
from . import icons
from . import widgets as W
from .base import SLOW_NOTE
from .pages import live_sig
from .widgets import Avatar, Badge, Bar, Button, Icon, IconTile, Image, Node, Run, Section, Ticker, box, elided, hbox, icon_button, label, px, rich, vbox

if TYPE_CHECKING:
    from .app import Controller

WIDTH = 360
MARGIN = 12            # from the edges of the screen's free area, above the taskbar
PAGES = {"live": "Live", "approvals": "Approvals", "problems": "Problems"}
TILES = {"waiting": ("approval", "warn"), "blocked": ("window", "bad"), "closed": ("x", "bad"), "timeout": ("clock", "warn")}


def display_key(d: wx.Display) -> str:
    """A screen, as the portal remembers its corner on it: its name and where it is."""
    g = d.GetGeometry()
    return f"{d.GetName()}|{g.x},{g.y},{g.width},{g.height}"


def screen(key: str) -> wx.Display:
    """The screen with that key, or the main one when it is gone."""
    shown = [wx.Display(i) for i in range(wx.Display.GetCount())]
    return next((d for d in shown if display_key(d) == key), next((d for d in shown if d.IsPrimary()), shown[0]))


def ago(t: float, now: float) -> str:
    """4 s ago, just now, 3 min ago: the portal's header counts the seconds."""
    s = now - t
    if s < 2:
        return "just now"
    return f"{int(s)} s ago" if s < 60 else w.ago(t, now)


class Grip(W.Fixed):
    """The handle at the left of the top bar. The bar drags the portal; here the arrow keys move it too."""

    focusable = True
    click_focus = False
    role = W.ROLE_BUTTON

    def __init__(self, move: Callable[[int], None]):
        super().__init__(24, 32)
        self.move = move
        self.name = "Move the portal"
        self.description = "Arrow keys move it to another corner of the screen."

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        s = px(16)
        gc.DrawBitmap(icons.bitmap("grip", W.THEME.c["secondary"], s), (w - s) / 2, (h - s) / 2, s, s)

    def key_down(self, code: int) -> bool:
        if code in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN):
            self.move(code)
            return True
        return False


class FootLink(Button):
    """A count in the footer that opens its page: 2 in line, 2 need you."""

    def __init__(self, icon: str, text: str, tone: str, on_click: Callable[[], None]):
        super().__init__(text, "link", icon, on_click=on_click, role_font="caption_strong" if tone else "caption")
        self.tone = tone

    def _icon_size(self) -> int:
        return px(14)

    def height(self, w: int) -> int:
        return W.line_h(self.font_role)

    def colours(self) -> tuple[str | None, str | None, str]:
        return None, None, W.tone_hex(self.tone)


class PortalRoot(W.Root):
    """The portal's tree: the design's width, the height its state needs; the window keeps its corner, so at a bottom
    corner it grows upward."""

    def __init__(self, frame: "PortalWindow", node: W.Win):
        self.frame = frame
        super().__init__(frame, node)

    def layout(self) -> None:
        self.pending = False
        width = px(WIDTH)
        height = self.node.height(width)
        self.frame.fit(width, height)
        self.frame.Freeze()
        try:
            self.node.place(0, 0, width, height)
        finally:
            self.frame.Thaw()


class PortalWindow(wx.Frame):
    def __init__(self, app: "Controller", logo: wx.Image):
        super().__init__(None, title="Activity Monitor portal",
                         style=wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.app = app
        self.ticker = Ticker()
        self.corner = "br"     # t or b, then l or r
        self.display = ""
        self.grab = wx.Point()
        self.view = Portal("idle")
        card = box("portal", (1, 1, 1, 1))  # inside its border, as the design's card
        head = box("plain", (4, 4, 4, 4), 8, horizontal=True)
        head.on_drag = self.drag  # type: ignore[assignment]
        head.add(Grip(self.nudge))
        head.add(Image(logo, 16))
        self.status = head.add(Section())
        head.spring()
        self.expand = head.add(icon_button("expand", "Expand", app.expand_portal, size=32))
        head.add(icon_button("close", "Hide", app.close_portal, size=32, name="Hide the portal until an assistant starts again"))
        card.add(head)
        self.body = card.add(Section())
        self.foot = card.add(Section())
        self.root = PortalRoot(self, card)
        self.Bind(wx.EVT_CHAR_HOOK, W.on_char_hook(self))
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.retheme()

    # -------------------------------------------------------- where it sits

    def fit(self, width: int, height: int) -> None:
        a = screen(self.display).GetClientArea()
        m = px(MARGIN)
        x = a.x + m if self.corner[1] == "l" else a.x + a.width - width - m
        y = a.y + m if self.corner[0] == "t" else a.y + a.height - height - m
        if self.GetRect() != wx.Rect(x, y, width, height):
            self.SetSize(x, y, width, height)

    def drag(self, kind: str, at: wx.Point) -> None:
        if kind == "down":
            self.grab = at - self.GetPosition()
        elif kind == "move":
            self.Move(at - self.grab)
        else:  # let go: the nearest corner of the screen it is on
            r = self.GetRect()
            x, y = r.x + r.width // 2, r.y + r.height // 2
            i = wx.Display.GetFromPoint(wx.Point(x, y))
            d = wx.Display(i if i != wx.NOT_FOUND else 0)
            a = d.GetClientArea()
            self.moved(d, ("t" if y < a.y + a.height // 2 else "b") + ("l" if x < a.x + a.width // 2 else "r"))

    def nudge(self, code: int) -> None:
        v, h = self.corner
        if code in (wx.WXK_LEFT, wx.WXK_RIGHT):
            h = "l" if code == wx.WXK_LEFT else "r"
        else:
            v = "t" if code == wx.WXK_UP else "b"
        self.moved(screen(self.display), v + h)

    def moved(self, d: wx.Display, corner: str) -> None:
        self.display, self.corner = display_key(d), corner
        self.app.portal_moved(self.display, corner)
        self.root.layout()

    def open(self, activate: bool) -> None:
        """Shows it in its corner of its screen. Opened by itself it takes no focus, so typing in QuickBooks goes on."""
        settings = self.app.portal_settings()
        self.display = display_key(screen(str(settings.get("display") or "")))
        corners = settings.get("corners")
        corner = corners.get(self.display) if isinstance(corners, dict) else None
        self.corner = corner if corner in ("tl", "tr", "bl", "br") else "br"
        self.refresh()
        self.root.layout()
        if activate:
            self.Show()
            self.Raise()
            W.tab(self, True)  # the grip, the first stop
        else:
            self.ShowWithoutActivating()
        round_corners(self)

    def on_close(self, e: wx.CloseEvent) -> None:
        if e.CanVeto():  # Alt+F4 hides it, as its close button does
            e.Veto()
            self.app.close_portal()
            return
        e.Skip()

    # -------------------------------------------------------- what it shows

    def refresh(self) -> None:
        app = self.app
        v = self.view = app.portal_view or app.model.portal()
        now = app.now()
        e = app.epoch
        self.expand.name = f"Expand: open the window on {PAGES.get(v.page, 'Overview')}"
        self.status.show_built((e, v.state, v.badge, v.since), lambda: self.build_status(v))
        minutes = int((now - v.since) // 60) if v.state == "timeout" else int((v.expires - now) // 60) if v.state == "waiting" else 0
        hides = app.portal_hides_at() if v.state in ("done", "idle") else None
        self.body.show_built((e, v.state, v.title, v.detail, v.text, live_sig(v.cmd), v.who, minutes, hides is not None,
                              tuple(app.busy)), lambda: self.build_body(v))
        self.foot.show_built((e, v.line, v.need if v.state != "waiting" else 0), lambda: self.build_foot(v))

    def tick(self) -> None:
        self.ticker.tick()

    def build_status(self, v: Portal) -> Node | None:
        if v.badge is None:
            return None
        row = hbox(gap=8)
        row.add(Badge(*v.badge))
        if v.state in ("running", "stopping", "line"):
            t = row.add(self.ticker.add(label("", "caption", "secondary"), lambda: w.span(self.app.now() - v.since)))
            t.tip = f"Started {w.clock(v.since, seconds=True)}"
        elif v.state in ("stopped", "done"):
            t = row.add(self.ticker.add(label("", "caption", "secondary"), lambda: ago(v.since, self.app.now())))
            t.tip = w.exact(v.since)
        return row

    def build_body(self, v: Portal) -> Node:
        app = self.app
        if v.state in TILES:
            return self.build_tile(v)
        col = vbox((14, 2, 14, 14 if v.state == "done" else 12), 8)
        if v.state == "idle" or v.who is None:
            col.add(label("Nothing runs now. The portal follows an assistant while it works in QuickBooks.", "caption", "secondary",
                          wrap=True))
            return col
        row = hbox(gap=10)
        row.add(Avatar(v.who, 24))
        names = vbox(gap=0)
        names.add(elided(v.title, "strong"))
        names.add(elided(v.detail, "caption", "secondary"))
        row.add(names, 1)
        col.add(row)
        c = v.cmd
        if v.state in ("running", "stopping") and c is not None:
            known = c.percent is not None
            col.add(Bar(c.percent / 100 if known else None, "accent", 4, name=c.title))
            if known:
                nums = hbox(gap=12)
                nums.add(elided(f"{w.num(c.read)} of {w.num(c.total)} records", "caption"), 1)
                nums.add(label(f"{c.percent}%", "caption", "secondary"))
                col.add(nums)
            else:
                col.add(elided(c.doing or "Waiting for QuickBooks to answer…", "caption"))
                slow = col.add(box("plain", gap=4, horizontal=True))
                slow.add(Icon("clock", "warn", 12))
                slow.add(rich([Run("Slow", "caption_strong", "warn"), Run(f" · {SLOW_NOTE}", "caption", "warn")]), 1)
                self.ticker.every(slow, lambda: slow.show(c.elapsed(app.now()) >= SLOW))
            if v.state == "stopping":
                note = box("stopping", (10, 8, 10, 8), 8, horizontal=True)
                note.add(Icon("stop_square", "secondary"), align="top")
                when = "this page is read" if c.page else "QuickBooks answers"
                note.add(rich([Run("Stopping after this step.", "caption_strong"),
                               Run(f" {c.who.short} stops when {when}, then waits until you resume.", "caption")]), 1)
                note.name = "Stopping after this step."
                col.add(below(note))
            elif c.stoppable:
                b = Button("Stop after this step", "quiet", "stop_square", c.stop_note, lambda: app.stop(c), height=32)
                b.key = f"stop:{c.pid}"
                col.add(below(b), align="left")
        elif v.state == "stopped" and v.resume is not None:
            folder, info = v.resume
            col.add(label(v.text, "body", wrap=True))
            short = v.who.short if not v.who.unknown else "the assistant"
            b = Button(f"Resume {short}", "accent", "resume", on_click=lambda: app.resume(folder, info), height=32)
            b.key = "resume"
            col.add(below(b), align="left")
        elif v.state == "line" and c is not None:
            line = hbox(gap=10)
            bar = line.add(Bar(min(1.0, c.elapsed(app.now()) / LINE_WAIT), "warn", 4, name="Time waited"), 1)
            self.ticker.every(bar, lambda: bar.set(min(1.0, c.elapsed(app.now()) / LINE_WAIT)))
            line.add(self.ticker.add(label("", "caption", "secondary"), lambda: f"waited {w.span(c.elapsed(app.now()))} of {LINE_WAIT} s"))
            col.add(line)
        elif v.state == "done" and app.portal_hides_at() is not None:
            col.add(self.ticker.add(label("", "caption", "secondary", wrap=True),
                                    lambda: f"Hides in {max(0, math.ceil((app.portal_hides_at() or 0) - app.now()))} s unless another "
                                            "request starts."))
        return col

    def build_tile(self, v: Portal) -> Node:
        """What waits for someone: an icon on its colour, what happened and what to do."""
        col = vbox((14, 2, 14, 14), 10)
        row = hbox(gap=12)
        row.add(IconTile(*TILES[v.state], 32), align="top")
        names = vbox(gap=0)
        now = self.app.now()
        if v.state == "timeout":
            names.add(rich([Run(v.title, "strong"), Run(f" · for {w.span(now - v.since)}", "body")]))
        else:
            names.add(label(v.title, "strong", wrap=True))
        detail = v.detail
        if v.state == "waiting":
            left = v.expires - now
            detail += f" · the first expires in {w.span(left) if left >= 60 else 'under a minute'}" if left > 0 else " · the first has expired"
        names.add(label(detail, "caption", "secondary", wrap=True))
        row.add(names, 1)
        col.add(row)
        if v.state == "waiting":
            col.add(Button("Review", "accent", on_click=self.app.expand_portal, height=32), align="left")
        elif v.state in ("blocked", "closed"):
            col.add(Button("See the problem", "quiet", on_click=self.app.expand_portal, height=32), align="left")
        return col

    def build_foot(self, v: Portal) -> Node | None:
        need = v.need if v.state != "waiting" else 0
        if not v.line and not need:
            return None
        f = box("portalFoot", (14, 9, 14, 8), 16, horizontal=True)  # 8 under its top border
        if v.line:
            f.add(FootLink("line", f"{w.num(v.line)} in line", "", lambda: self.app.expand_portal("live")))
        if need:
            f.add(FootLink("need", f"{w.num(need)} need you", "warn", lambda: self.app.expand_portal("approvals")))
        f.spring()
        return f

    def retheme(self) -> None:
        self.SetBackgroundColour(W.c("portal"))
        W.refresh_all(self)


def below(node: Node) -> Node:
    """A button or note set 2 px further down than the column's gap, as the design has it."""
    b = vbox((0, 2, 0, 0))
    b.add(node, align="left" if not node.fill_x else "")
    return b


def round_corners(frame: wx.Frame) -> None:
    """Rounded corners where Windows 11 draws them; earlier Windows keep the square border."""
    if sys.platform != "win32":
        return
    import ctypes

    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(frame.GetHandle(), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except OSError:
        pass

