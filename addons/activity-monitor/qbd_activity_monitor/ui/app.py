"""The monitor at run time: the model read each second, the window, the portal and the tray, actions run off the
screen's thread, notifications, one monitor per Windows user, and light or dark as Windows has it.

Notifications can show on a locked screen and the portal stays on screen while people work, so both name the
assistant and what waits, never an amount, a customer or a record. The pipe that keeps this monitor to one per
Windows user is local and named after that user, and the only thing it understands is "show"."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from typing import Any, Callable

import wx
import wx.adv

from qbd_agent_kit import api

from .. import actions
from .. import words as w
from ..feed import Folder
from ..model import ALL, Approval, ChangeRow, LiveCmd, Model, Portal
from . import icons
from . import widgets as W
from .flyout import Flyout
from .portal import PortalWindow
from .theme import Theme, windows_dark
from .window import Window

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
POLL_MS = 1000
QB_EVERY = 5.0       # seconds between looks at whether QuickBooks runs
NOTICE_KEEP = 120    # seconds a message about an action stays on Approvals
MARKS_EVERY = 60     # seconds between saves of the logs' marks
HIDE_AFTER = 30      # seconds after the last request ended, the portal that opened by itself hides
TRAY_MARKS = {"warn_mark": "#E8A300", "bad_mark": "#D13438", "info_mark": "#0F6CBD"}


def quickbooks_running() -> bool | None:
    """Whether QuickBooks' program runs on this PC; None where that cannot be told."""
    if sys.platform != "win32":
        return None
    names = {name.lower() for _, name in api.windows_processes().values()}
    return bool(names & {"qbw.exe", "qbw32.exe"}) if names else None


def reduced_motion() -> bool:
    """Windows' "Animation effects" setting, off means no moving bars."""
    if sys.platform != "win32":
        return False
    import ctypes

    on = ctypes.c_int(1)
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
    except OSError:
        return False
    return not on.value


def address() -> tuple[str, str]:
    """This Windows user's pipe; on Linux, where the monitor is developed, an abstract socket."""
    name = "qbd-activity-monitor-" + re.sub(r"[^A-Za-z0-9_.-]", "_", api.who_am_i())
    return (rf"\\.\pipe\{name}", "AF_PIPE") if sys.platform == "win32" else ("\0" + name, "AF_UNIX")


def another_running() -> bool:
    """True when this Windows user already runs a monitor; it is asked to show its window."""
    try:
        with Client(*address()) as conn:
            conn.send_bytes(b"show")
    except OSError:
        return False
    return True


def double_click_ms() -> int:
    """The most time between the two clicks of a double click, as Windows has it."""
    return wx.SystemSettings.GetMetric(wx.SYS_DCLICK_MSEC)


def icon_bundle(image: wx.Image) -> wx.IconBundle:
    bundle = wx.IconBundle()
    for size in (16, 20, 24, 32, 48, 64, 256):
        icon = wx.Icon()
        icon.CopyFromBitmap(wx.Bitmap(image.Scale(size, size, wx.IMAGE_QUALITY_HIGH)))
        bundle.AddIcon(icon)
    return bundle


def dark_title_bar(frame: wx.Frame, dark: bool) -> None:
    """A dark title bar over a dark window, where Windows draws one (Windows 10 1809 and later)."""
    if sys.platform != "win32":
        return
    import ctypes

    on = ctypes.c_int(1 if dark else 0)
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, and its number before Windows 10 20H1
        try:
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(frame.GetHandle(), attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                return
        except OSError:
            return


class Every(wx.Timer):
    def __init__(self, fn: Callable[[], None]):
        super().__init__()
        self.fn = fn

    def Notify(self) -> None:
        self.fn()


class Tray(wx.adv.TaskBarIcon):
    """The icon by the clock: a click opens the panel, a double click the window, a right click the menu."""

    def __init__(self, app: "Controller"):
        super().__init__()
        self.app = app
        self.double_at = 0.0
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self.on_click)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_double_click)
        self.Bind(wx.adv.EVT_TASKBAR_BALLOON_CLICK, lambda e: app.show(app.message_page))

    def on_click(self, e: wx.adv.TaskBarIconEvent) -> None:
        if time.time() - self.double_at > double_click_ms() / 1000:  # not the release that ends a double click
            self.app.tray_clicked(wx.GetMousePosition())

    def on_double_click(self, e: wx.adv.TaskBarIconEvent) -> None:
        self.double_at = time.time()
        self.app.tray_double_clicked()

    def CreatePopupMenu(self) -> wx.Menu:
        return self.app.tray_menu()


class Controller:
    def __init__(self, model: Model, tray: bool = True, dark: bool | None = None, single: bool = True):
        self.model = model
        model.poll()
        files = model.files()
        self.pick = model.default_file() if files else ALL
        self.narrow = False
        self.epoch = 0
        self.updated = model.now()
        self.busy: set[str] = set()
        self.notices: list[tuple[str, str, str, float]] = []
        self.history: list[str] = []
        self.page = "overview"
        self.message_page = "overview"
        self.forced_dark = dark
        self.dark: bool | None = None
        self.qb_state: bool | None = None
        self.model.quickbooks = lambda: self.qb_state
        W.REDUCED_MOTION = reduced_motion()
        self.apply_theme()
        self.logo = wx.Image(ICON)
        self.icons = icon_bundle(self.logo)
        self.window = Window(self, self.icons)
        self.flyout = Flyout(self, self.logo)
        self.portal_view: Portal | None = None
        self.portal = PortalWindow(self, self.logo)
        self.by_itself = False        # the portal opened by itself, so it hides once the requests are done
        self.running_pids: set[int] = set()
        last = model.last_request()
        self.last_seen = (last.src, last.line) if last is not None else None
        dark_title_bar(self.window, bool(self.dark))
        if single:
            self.listen()
        self.tray: Tray | None = None
        self.tray_shown: tuple[str, str] = ("", "")
        self.opening: wx.CallLater | None = None  # the tray's panel, after a click
        if tray and wx.adv.TaskBarIcon.IsAvailable():
            self.tray = Tray(self)
            self.chrome()
        self.known_drafts = {a.token for a in model.approvals(ALL)[0]}
        self.known_blocked = self.blocked_key()
        self.marks_at = time.time()
        self.timer = Every(self.poll)
        if sys.platform == "win32":
            threading.Thread(target=self.watch_quickbooks, daemon=True).start()

    # ------------------------------------------------------------ look

    def apply_theme(self) -> bool:
        """Light or dark as Windows has it; True when that changed."""
        dark = self.forced_dark
        if dark is None:
            dark = windows_dark()
        if dark is None:
            dark = wx.SystemSettings.GetAppearance().IsDark()
        if dark == self.dark:
            return False
        self.dark = dark
        W.set_theme(Theme(dark=dark))
        return True

    def retheme(self) -> None:
        self.epoch += 1
        self.window.retheme()
        self.flyout.retheme()
        self.portal.retheme()
        dark_title_bar(self.window, bool(self.dark))
        self.tray_shown = ("", "")
        self.refresh()

    def set_narrow(self, narrow: bool) -> None:
        if narrow != self.narrow:
            self.narrow = narrow
            wx.CallAfter(self.refresh)

    # ------------------------------------------------------------ the loop

    def start(self, show_window: bool, page: str = "overview") -> None:
        self.show(page, bring=show_window)
        self.timer.Start(POLL_MS)

    def watch_quickbooks(self) -> None:
        while True:
            try:
                self.qb_state = quickbooks_running()
            except OSError:
                self.qb_state = None
            time.sleep(QB_EVERY)

    def poll(self) -> None:
        if self.forced_dark is None and self.apply_theme():
            self.retheme()
        changed = self.model.poll()
        if changed:
            self.updated = self.model.now()
            self.notify()
        kept = [n for n in self.notices if time.time() - n[3] < NOTICE_KEEP]
        if len(kept) != len(self.notices):
            self.notices = kept
        self.follow()
        self.refresh()
        if time.time() - self.marks_at >= MARKS_EVERY:
            self.save_marks()

    def poll_now(self) -> None:
        self.poll()

    def save_marks(self) -> None:
        self.marks_at = time.time()
        self.model.state.data["marks"] = self.model.feed.marks()
        self.save_state()

    def save_state(self) -> None:
        self.model.state.dirty = True
        try:
            self.model.state.save()
        except OSError:
            pass

    def refresh(self) -> None:
        page = self.window.current()
        page.refresh()
        page.tick()
        self.chrome()
        if self.flyout.IsShown():
            self.flyout.refresh()
            self.flyout.tick()
        if self.portal.IsShown():
            self.portal.refresh()
            self.portal.tick()

    def chrome(self) -> None:
        """The pane's counts, the narrow bar and the tray icon."""
        self.window.top(self.window.current())
        running, _ = self.model.live(self.pick)
        waiting = self.model.approvals(self.pick)[0]
        self.window.counts(1 if running else 0, len(waiting), len(self.model.new_problems()))
        if self.tray is not None:
            t = self.model.tray()
            if (t.state, t.tooltip) != self.tray_shown:
                self.tray_shown = (t.state, t.tooltip)
                self.tray.SetIcon(icons.tray_icon(self.logo, t.state, TRAY_MARKS), t.tooltip)

    def changed(self) -> None:
        self.chrome()

    # ------------------------------------------------------------ the portal

    def portal_settings(self) -> dict:
        s = self.model.state.data.get("portal")
        if not isinstance(s, dict):
            s = self.model.state.data["portal"] = {}
        return s

    def portal_auto(self) -> bool:
        return self.portal_settings().get("auto", True) is not False

    def set_portal_auto(self, on: bool) -> None:
        self.portal_settings()["auto"] = on
        self.save_state()

    def portal_moved(self, display: str, corner: str) -> None:
        s = self.portal_settings()
        corners = s.get("corners") if isinstance(s.get("corners"), dict) else {}
        s["corners"] = {**corners, display: corner}
        s["display"] = display
        self.save_state()

    def portal_hides_at(self) -> float | None:
        """When the portal that opened by itself hides: 30 s after the last request ended."""
        v = self.portal_view
        if not self.by_itself or v is None or v.state not in ("done", "idle"):
            return None
        return v.since + HIDE_AFTER

    def follow(self) -> None:
        """The portal opens by itself when an assistant starts a request and the window is not on screen, without the
        keyboard focus; opened that way, it hides 30 s after the last request ended unless something needs someone.
        A request too short to be seen running counts as one started when nothing ran before it."""
        v = self.portal_view = self.model.portal()
        running, waiting = self.model.live(ALL)
        pids = {c.pid for c in ([running] if running else []) + waiting}
        last = self.model.last_request()
        mark = (last.src, last.line) if last is not None else None
        started = bool(pids - self.running_pids) or (mark != self.last_seen and not self.running_pids)
        self.running_pids, self.last_seen = pids, mark
        if started and self.portal_auto() and not self.window.on_screen() and not self.portal.IsShown():
            self.by_itself = True
            self.portal.open(activate=False)
        hides = self.portal_hides_at()
        if self.portal.IsShown() and hides is not None and self.now() >= hides:
            self.portal.Hide()
            self.by_itself = False

    def show_portal(self) -> None:
        """The window's portal button and the tray menu: the window goes to the tray and the portal opens."""
        if self.window.IsShown() and not self.window.IsIconized():
            if self.tray is not None:
                self.window.Hide()
            else:
                self.window.Iconize(True)
        self.by_itself = False
        self.portal_view = self.model.portal()
        self.portal.open(activate=W.KEYBOARD)

    def expand_portal(self, page: str = "") -> None:
        """Expand: the window on the page that fits; the portal closes."""
        self.portal.Hide()
        self.by_itself = False
        self.show(page or (self.portal_view.page if self.portal_view else "overview"))

    def close_portal(self) -> None:
        """Hidden until an assistant starts again."""
        self.portal.Hide()
        self.by_itself = False

    # ------------------------------------------------------------ notifications (TR-5): nothing confidential

    def blocked_key(self) -> Any:
        r = self.model._unsettled(ALL, w.BLOCKED)
        return (r.src, r.line) if r is not None else None

    def notify(self) -> None:
        drafts = self.model.approvals(ALL)[0]
        new = [a for a in drafts if a.token not in self.known_drafts]
        self.known_drafts = {a.token for a in drafts}
        blocked = self.blocked_key()
        if new:
            who = new[0].who.short if len({a.who.key for a in new}) == 1 else "Assistants"
            self.toast(f"{who} {'has a change' if len(new) == 1 else f'have {len(new)} changes'} waiting for your approval.",
                       "approvals")
        elif blocked is not None and blocked != self.known_blocked:
            self.toast("A QuickBooks window is open and waiting for someone. Close it, then retry.", "problems")
        self.known_blocked = blocked

    def toast(self, text: str, page: str) -> None:
        if self.tray is None:
            return
        self.message_page = page
        if sys.platform == "win32":
            self.tray.ShowBalloon("Activity Monitor", text, 8000)
        else:
            n = wx.adv.NotificationMessage("Activity Monitor", text)
            n.Bind(wx.adv.EVT_NOTIFICATION_MESSAGE_CLICK, lambda e: self.show(page))
            n.Show(8)
            self.last_toast = n  # kept until the next one, for its click

    # ------------------------------------------------------------ the tray

    def tray_menu(self) -> wx.Menu:
        m = wx.Menu()

        def item(text: str, fn: Callable[[], None]) -> None:
            m.Bind(wx.EVT_MENU, lambda e: fn(), m.Append(wx.ID_ANY, text))

        item("Open activity", lambda: self.show("overview"))
        item("Show the portal", self.show_portal)
        m.AppendSeparator()
        auto = m.AppendCheckItem(wx.ID_ANY, "Open the portal when an assistant starts")
        auto.Check(self.portal_auto())
        m.Bind(wx.EVT_MENU, lambda e: self.set_portal_auto(e.IsChecked()), auto)
        start = m.AppendCheckItem(wx.ID_ANY, "Start with Windows")
        on = actions.starts_with_windows()
        start.Check(bool(on))
        start.Enable(on is not None)
        m.Bind(wx.EVT_MENU, lambda e: actions.set_start_with_windows(e.IsChecked()), start)
        m.AppendSeparator()
        item("Quit", self.quit)
        return m

    def tray_clicked(self, at: wx.Point) -> None:
        """The panel opens once a double click can no longer come. Opened at once, it would take the focus, which closes
        Windows' hidden icons, and the second click of a double click there would land on the window under them, which
        may be QuickBooks."""
        if self.flyout.IsShown():
            self.flyout.Hide()
        elif not self.flyout.just_closed():
            self.opening = wx.CallLater(double_click_ms(), self.flyout.show_near, at)

    def tray_double_clicked(self) -> None:
        if self.opening is not None:
            self.opening.Stop()
        self.show("overview")

    def keeps_running(self) -> bool:
        return self.tray is not None

    def listen(self) -> None:
        try:
            listener = Listener(*address())
        except OSError:
            return

        def serve() -> None:
            while True:
                try:
                    with listener.accept() as conn:
                        if conn.poll(1) and conn.recv_bytes(16) == b"show":
                            wx.CallAfter(self.show, self.page)
                except (OSError, EOFError):
                    continue

        threading.Thread(target=serve, daemon=True).start()

    def quit(self) -> None:
        self.save_marks()
        self.timer.Stop()
        if self.tray is not None:
            self.tray.RemoveIcon()
            self.tray.Destroy()
            self.tray = None
        for frame in (self.portal, self.flyout, self.window):
            frame.Destroy()
        # wx deletes the frames when it is next idle, then ends its loop. Ended now, the loop would leave them to be
        # deleted as Python exits, which crashes on Windows; the timer ends it if something else keeps it going.
        wx.CallLater(2000, wx.GetApp().ExitMainLoop)

    # ------------------------------------------------------------ moving around

    def now(self) -> float:
        return self.model.now()

    def show(self, name: str, bring: bool = True, **kw: Any) -> None:
        pages = self.window.pages
        if name == "sessions" and "period" in kw:
            pages["sessions"].period, pages["sessions"].limit = kw["period"], 25  # type: ignore[attr-defined]
        if name != self.page:
            self.history = (self.history + [self.page])[-20:]
        self.page = name
        self.window.select(name)
        page = pages[name]
        if name == "approvals":
            page.open(kw.get("token", ""))  # type: ignore[attr-defined]
        else:
            page.refresh()
        page.shown()
        self.chrome()
        if bring:
            self.portal.Hide()  # the window and the portal: one of them is on screen at a time
            self.by_itself = False
            if self.window.IsIconized():
                self.window.Iconize(False)
            self.window.Show()
            self.window.Raise()

    def back(self) -> None:
        if self.history:
            name = self.history.pop()
            self.page = name
            self.window.select(name)
            self.window.pages[name].refresh()

    def set_pick(self, key: str) -> None:
        self.pick = key
        self.refresh()

    def open_session(self, sid: str | None, request: str = "") -> None:
        if not sid:  # its requests are no longer in the logs the monitor reads
            self.show("sessions")
            return
        self.window.pages["session"].open(sid, request)  # type: ignore[attr-defined]
        self.show("session")

    def open_session_by(self, who: Any, session: str, t: float) -> None:
        self.open_session(self.model.session_at(who, session, t))

    def open_session_of(self, cmd: LiveCmd) -> None:
        self.open_session_by(cmd.who, str(cmd.info.get("session") or ""), cmd.started)

    def open_call(self, rec: Any) -> None:
        who = self.model.assistants.of(rec.e)
        sid = self.model.session_at(who, str(rec.e.get("session") or ""), rec.t)
        self.open_session(sid, f"request:{rec.src}:{rec.line}")

    # ------------------------------------------------------------ actions (release 2), off the screen's thread

    def busy_with(self, key: str) -> bool:
        return key in self.busy

    def notice(self, tone: str, title: str, text: str) -> None:
        self.notices = [n for n in self.notices if n[1] != title] + [(tone, title, text, time.time())]
        self.refresh()

    def drop_notice(self, n: tuple[str, str, str, float]) -> None:
        self.notices = [x for x in self.notices if x != n]
        self.refresh()

    def run(self, key: str, work: Callable[[], Any], after: Callable[[Any], None]) -> None:
        if key in self.busy:
            return
        self.busy.add(key)
        self.refresh()

        def go() -> None:
            try:
                result = work()
            except Exception as e:  # shown to the person, never lost
                result = {"error": {"code": "monitor", "message": f"{type(e).__name__}: {e}"}}
            wx.CallAfter(self.finished, key, after, result)

        threading.Thread(target=go, daemon=True).start()

    def finished(self, key: str, after: Callable[[Any], None], result: Any) -> None:
        self.busy.discard(key)
        after(result)
        self.poll()

    def sample_only(self, folder: Folder) -> bool:
        if folder.sample:
            self.notice("info", "Sample data", "This is the monitor's sample data: nothing is saved, stopped or undone from it.")
        return folder.sample

    def approve(self, a: Approval) -> None:
        if self.sample_only(a.folder):
            return
        self.run(f"approve:{a.token}", lambda: actions.approve(a), lambda r: self.approved(a, r))

    def approved(self, a: Approval, r: dict) -> None:
        if "error" in r:
            self.notice("bad", f"Not saved: {a.title}", str(r["error"].get("message") or "qbd refused it."))
            return
        counts = r.get("counts") if isinstance(r.get("counts"), dict) else None
        if counts:
            text = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()).replace("done", "saved")
            self.notice("ok" if r.get("done") else "warn", f"Saved from the import: {a.title}", f"{text}. Each record is under Changes.")
        else:
            self.notice("ok", f"Saved: {a.title}", f"Saved to QuickBooks. {a.who.short} learns it when it asks next; Changes has it, "
                        "with Undo where it is possible.")

    def reject(self, a: Approval) -> None:
        if self.sample_only(a.folder):
            return

        def after(r: dict) -> None:
            if "error" in r:
                self.notice("bad", f"Not rejected: {a.title}", str(r["error"].get("message")))
            else:
                self.notice("info", f"Rejected: {a.title}", f"Nothing was saved. {a.who.short} is told you said no when it asks next.")

        self.run(f"reject:{a.token}", lambda: actions.reject(a), after)

    def dismiss(self, a: Approval) -> None:
        if not self.sample_only(a.folder):
            actions.dismiss(a)
            self.poll()

    def undo_token(self, token: str) -> None:
        c = self.model.change_of(token)
        if c is not None:
            self.undo(c)

    def undo(self, c: ChangeRow) -> None:
        if self.sample_only(Folder(c.rec.folder, True, sample=self.model_sample(c.rec.folder))):
            return
        token = str(c.rec.e.get("token"))

        def after(r: dict) -> None:
            if "error" in r:
                self.notice("bad", f"Undo not prepared: {c.title}", str(r["error"].get("message")))
            else:
                self.notice("ok", f"Undo prepared: {c.title}", "It waits under Waiting for you: approve it to put the record back.")
                self.show("approvals")

        self.run(f"undo:{token}", lambda: actions.undo(c), after)

    def model_sample(self, path: str) -> bool:
        return any(f.sample for f in self.model.feed.folders if f.path == path)

    def stop(self, cmd: LiveCmd) -> None:
        if self.sample_only(cmd.folder):
            return
        actions.stop(cmd)
        self.notice("info", f"Stopping {cmd.who.short} after this step",
                    f"{cmd.title} ends when QuickBooks answers its current request; {cmd.who.short}'s next command waits until you "
                    "click Resume.")
        self.poll()

    def resume(self, folder: Folder, info: dict) -> None:
        if self.sample_only(folder):
            return
        actions.resume(folder, info)
        self.poll()
