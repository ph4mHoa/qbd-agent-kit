"""The main window: the navigation pane with its counts, the pages, and a narrow layout under 760 px where a menu
button takes the pane's place."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import wx

from . import icons
from . import widgets as W
from .base import NARROW, PORTAL_TIP, Page
from .pages import PAGES
from .widgets import Bubble, Icon, Node, Section, box, c, elided, hbox, icon_button, label, menu_text, px

if TYPE_CHECKING:
    from .app import Controller

NAV = [("overview", "Overview", "home"), ("live", "Live", "running"), ("sessions", "Sessions", "list"),
       ("approvals", "Approvals", "approval"), ("changes", "Changes", "history"), ("problems", "Problems", "warning")]
SAFETY = ("safety", "Safety", "shield")
W.LOOKS["navItem"] = W.Look(None, None, 4, hover="hover")


class NavItem(W.Surface):
    """One place in the pane: icon, name, and what waits there (1 running, a count)."""

    focusable = True
    click_focus = False  # a click does not leave the focus in the pane; Tab does
    role = W.ROLE_BUTTON

    def __init__(self, key: str, text: str, icon_name: str, on_click: Callable[[], None]):
        super().__init__("navItem", (12, 0, 10, 0), 12, horizontal=True)
        self.page, self.text_ = key, text
        self.on_click = on_click
        self.add(Icon(icon_name, "", 16))
        self.label_ = self.add(elided(text, "body"), 1)
        self.extra = self.add(Section())
        self.extra_sig: object = None
        self.checked = False
        self.name = text

    def height(self, w: int) -> int:
        return px(40)

    def fill(self) -> str | None:
        if self.checked:
            return "selected"
        return super().fill()

    def set_checked(self, on: bool) -> None:
        if self.checked != on:
            self.checked = on
            self.label_.set_role("strong" if on else "body")
            self.refresh()

    def set_extra(self, kind: str, n: int) -> None:
        sig = (kind, n)
        if sig == self.extra_sig:
            return
        self.extra_sig = sig

        def build() -> Node | None:
            if n and kind == "running":
                return label(f"{n} running", "caption_strong", "accent")
            if n:
                return Bubble(n, "accent" if kind == "answer" else "neutral")
            return None

        self.extra.show_built(sig, build)
        self.name = (f"{self.text_}, {n} running" if kind == "running" else
                     f"{self.text_}, {n} {'waiting for you' if kind == 'answer' else 'new'}") if n else self.text_

    def compact(self, on: bool) -> None:
        self.label_.show(not on)
        self.extra.show(not on)
        self.tip = self.text_ if on else ""
        if self.win:
            W.set_tip(self.win, self.tip)

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        if self.checked:  # the selection mark at the left
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(c("accent")))
            gc.DrawRoundedRectangle(0, (h - px(16)) / 2, px(3), px(16), px(1.5))


class MenuButton(W.Button):
    """The menu button, with a dot when something waits for the person."""

    def __init__(self, tooltip: str, on_click: Callable[[], None], name: str):
        super().__init__("", "subtle", "menu", tooltip, on_click, size=36)
        self.name = name
        self.dot = False

    def set_dot(self, on: bool) -> None:
        if self.dot != on:
            self.dot = on
            self.refresh()

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        super().paint(gc, w, h)
        if self.dot:
            d = px(7)
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(c("accent")))
            gc.DrawEllipse((w + px(16)) / 2 - d + px(3), (h - px(16)) / 2 - px(3), d, d)


class Window(wx.Frame):
    def __init__(self, app: "Controller", icon: wx.IconBundle):
        super().__init__(None, title="Activity Monitor", style=wx.DEFAULT_FRAME_STYLE)
        self.app = app
        self.SetIcons(icon)
        self.SetMinClientSize(wx.Size(px(380), px(460)))
        area = wx.Display(max(0, wx.Display.GetFromWindow(self))).GetClientArea()
        self.SetClientSize(wx.Size(min(px(1280), area.width * 9 // 10), min(px(900), area.height * 9 // 10)))
        self.CentreOnScreen()
        self.body = wx.Window(self, style=wx.BORDER_NONE)
        self.narrow_nav = False
        self.dot = False
        # narrow: a bar with the menu button in place of the pane
        self.top_host = wx.Window(self.body, style=wx.BORDER_NONE)
        bar = box("topbar", (8, 6, 8, 6), 8, horizontal=True)
        self.menu_narrow = bar.add(MenuButton("Pages", self.open_menu, "Open the list of pages"))
        self.top_title = bar.add(elided("", "subtitle"), 1)
        self.top_picker = bar.add(Section())  # the page's company-file picker moves up here
        self.top_portal = bar.add(icon_button("portal", PORTAL_TIP, app.show_portal, size=36))
        self.top_root = W.Root(self.top_host, bar)
        self.top_host.Hide()
        # the pane
        self.nav_host = wx.Window(self.body, style=wx.BORDER_NONE)
        nav = box("nav", (8, 10, 8, 12), 4)
        self.menu_wide = nav.add(MenuButton("Show or hide the page names", self.toggle_nav, "Show or hide the page names"), align="left")
        nav.space(8)
        self.items: dict[str, NavItem] = {}
        for key, text, icon_name in NAV:
            nav.add(self.item(key, text, icon_name))
        nav.spring()
        nav.add(self.item(*SAFETY))
        self.nav_root = W.Root(self.nav_host, nav)
        # the pages
        self.pages_host = wx.Window(self.body, style=wx.BORDER_NONE)
        self.pages: dict[str, Page] = {}
        for cls in PAGES:
            page = cls(app, self.pages_host)
            page.scroll.Hide()
            self.pages[cls.name] = page
        self.page = self.pages["overview"]
        self.page.scroll.Show()
        self.body.Bind(wx.EVT_SIZE, lambda e: self.arrange())
        self.pages_host.Bind(wx.EVT_SIZE, lambda e: self.page.scroll.SetSize(self.pages_host.GetClientSize()))
        self.Bind(wx.EVT_CHAR_HOOK, W.on_char_hook(self))
        self.Bind(wx.EVT_CLOSE, self.on_close)
        keys = []
        for i, (key, _, _) in enumerate(NAV + [SAFETY], 1):
            keys.append((wx.ACCEL_CTRL, ord(str(i)), self.command(lambda k=key: app.show(k))))
        keys.append((wx.ACCEL_ALT, wx.WXK_LEFT, self.command(app.back)))
        keys.append((wx.ACCEL_NORMAL, wx.WXK_F5, self.command(app.poll_now)))
        self.SetAcceleratorTable(wx.AcceleratorTable(keys))
        self.retheme()

    def command(self, fn: Callable[[], None]) -> int:
        i = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: fn(), id=i)
        return i

    def item(self, key: str, text: str, icon_name: str) -> NavItem:
        it = NavItem(key, text, icon_name, lambda k=key: self.app.show(k))
        it.key = f"nav:{key}"
        self.items[key] = it
        return it

    # -------------------------------------------------------- layout

    def arrange(self) -> None:
        w, h = self.body.GetClientSize()
        if w <= 0 or h <= 0:
            return
        self.app.set_narrow(w < px(NARROW))
        narrow = self.app.narrow
        top = self.top_root.node.height(w) if narrow else 0
        nav = 0 if narrow else px(56 if self.narrow_nav else 248)
        self.top_host.Show(narrow)
        self.nav_host.Show(not narrow)
        self.top_host.SetSize(0, 0, w, top)
        self.nav_host.SetSize(0, top, nav, h - top)
        self.pages_host.SetSize(nav, top, w - nav, h - top)
        self.page.scroll.SetSize(self.pages_host.GetClientSize())
        self.top_root.layout()
        self.nav_root.layout()

    def current(self) -> Page:
        return self.page

    def select(self, name: str) -> None:
        page = self.pages[name]
        if page is not self.page:
            self.Freeze()
            try:
                self.page.scroll.Hide()
                self.page = page
                page.scroll.SetSize(self.pages_host.GetClientSize())
                page.scroll.Show()
            finally:
                self.Thaw()
        on = "sessions" if name == "session" else name
        for key, it in self.items.items():
            it.set_checked(key == on)

    def counts(self, running: int, answers: int, problems: int) -> None:
        self.items["live"].set_extra("running", running)
        self.items["approvals"].set_extra("answer", answers)
        self.items["problems"].set_extra("problem", problems)
        self.dot = bool(answers or problems)
        self.menu_narrow.set_dot(self.dot)
        self.menu_wide.set_dot(self.dot and self.narrow_nav)

    def open_menu(self) -> None:
        m = wx.Menu()
        for key, text, icon_name in NAV + [SAFETY]:
            extra = self.items[key].extra_sig
            n = extra[1] if isinstance(extra, tuple) else 0
            it = wx.MenuItem(m, wx.ID_ANY, menu_text(text + (f"  ({n})" if n else "")))
            it.SetBitmap(icons.bitmap(icon_name, W.THEME.c["text"], px(16)))
            m.Append(it)
            self.Bind(wx.EVT_MENU, lambda e, k=key: self.app.show(k), it)
        b = self.menu_narrow.win
        if b:
            self.PopupMenu(m, self.ScreenToClient(b.ClientToScreen(wx.Point(0, b.GetSize()[1]))))
        m.Destroy()

    def toggle_nav(self) -> None:
        """The pane with names, or only its icons."""
        self.narrow_nav = not self.narrow_nav
        for it in self.items.values():
            it.compact(self.narrow_nav)
        self.menu_wide.set_dot(self.dot and self.narrow_nav)
        self.arrange()

    def retheme(self) -> None:
        for host in (self.body, self.top_host, self.nav_host):
            host.SetBackgroundColour(c("mica"))
        self.pages_host.SetBackgroundColour(c("content"))
        for page in self.pages.values():
            page.scroll.SetBackgroundColour(c("content"))
            for n in page.col.walk():
                if isinstance(n, W.Field):
                    n.retheme()
        W.refresh_all(self)

    def top(self, page: Page) -> None:
        """Narrow: the page's name, its company-file picker and the portal button sit in the bar, as the design's
        Narrow board has them."""
        if not self.app.narrow:
            self.top_picker.show_built(None, lambda: None)
            return
        self.top_title.set_text(page.title or "Session")
        files = tuple((f.key, f.name, f.detail) for f in self.app.model.files())
        self.top_picker.show_built((page.name, self.app.pick, files), page.picker if page.uses_picker else lambda: None)

    def on_screen(self) -> bool:
        return self.IsShown() and not self.IsIconized()

    def on_close(self, e: wx.CloseEvent) -> None:
        if self.app.keeps_running() and e.CanVeto():  # the tray stays; the window comes back from it
            e.Veto()
            self.Hide()
            return
        self.app.quit()
