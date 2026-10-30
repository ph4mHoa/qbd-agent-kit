"""The Activity Monitor's window, tray panel and portal, built from the sample data: every page lays out wide and
narrow, light and dark, each state of the Portal board lays out, the portal opens, hides and moves as that board says,
a click on the tray icon opens the panel only once a double click can no longer come, Enter opens a line of the panel
and a change, a section built again hides the windows it drops before it destroys them, a node's window has its paint
style before it exists (both left Windows grey on the Live page), and quitting leaves wx no window to delete as Python
exits. An error raised while laying out or painting fails the test. Needs wxPython and a
screen (Xvfb on Linux)."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

try:
    import wx
except ImportError:
    wx = None

SCREEN = sys.platform == "win32" or bool(os.environ.get("DISPLAY"))

if wx is not None:
    from qbd_agent_kit.api import COMMANDS
    from qbd_activity_monitor.feed import KEEP_DAYS, Feed
    from qbd_activity_monitor.model import ALL, LINE_WAIT, LiveCmd, Model, Portal, State, Who
    from qbd_activity_monitor.sample import ENV, design_now, design_seen, write_sample
    from qbd_activity_monitor.ui import widgets as W
    from qbd_activity_monitor.ui.app import HIDE_AFTER, Controller, Tray, double_click_ms
    from qbd_activity_monitor.ui.base import NARROW
    from qbd_activity_monitor.ui.portal import MARGIN, PAGES, WIDTH

CLAUDE = ("a:claude (claude code)", "Claude (Claude Code)", "C", 0, "hoa", "Claude")
GEMINI = ("a:gemini (agy)", "Gemini (agy)", "G", 1, "hoa", "Gemini")


def pump() -> None:
    """Lets the screen catch up: what was laid out later, painted, and called after."""
    for _ in range(4):
        wx.SafeYield(None, True)


def wait(ms: int) -> None:
    """Runs the screen's loop for ms milliseconds, so what is due by then is called: outside that loop, yielding ran no
    timer on Windows."""
    wx.CallLater(ms, wx.GetApp().ExitMainLoop)
    wx.GetApp().MainLoop()


def words(node) -> list[str]:
    """The words a tree shows, its texts and its buttons'."""
    out = []
    for n in node.walk():
        if n.own_hidden():
            continue
        if isinstance(n, W.Text):
            out.append(n.text())
        elif isinstance(n, W.Button) and n.text:
            out.append(n.text)
    return out


def cmd(who: Who, title: str, since: float, **kw) -> LiveCmd:
    return LiveCmd(None, kw.pop("pid", 4242), {}, who, True, title, "", "", since, since, "", "", **kw)


def views(now: float) -> dict[str, Portal]:
    """Each state of the Portal board, as the model makes it."""
    claude, gemini = Who(*CLAUDE), Who(*GEMINI)
    run = ("Running", "info", "pulse")
    find = dict(step="Transactions", page=6, pages=14, read=1200, total=2800, percent=43, stop_note="Stops after this page.")
    running = cmd(claude, "Find duplicate entries", now - 18, **find)
    slow = cmd(gemini, "Who owes money (collections list)", now - 34, doing="QuickBooks is building the A/R aging report…")
    stopping = cmd(claude, "Find duplicate entries", now - 20, stop_asked=True, **find)
    detail = "Claude (Claude Code) · Transactions, page 6 of 14"
    return {
        "running": Portal("running", now - 18, claude, running, running.title, detail, badge=run, line=2, need=2),
        "no progress": Portal("running", now - 34, gemini, slow, slow.title, "Gemini (agy) · A/R aging report", badge=run, line=1),
        "stopping": Portal("stopping", now - 20, claude, stopping, stopping.title, detail, badge=run, line=2, need=2),
        "stopped": Portal("stopped", now - 1, claude, None, "Find duplicate entries", "Claude (Claude Code) · after step 3",
                          "Stopped by you. Nothing was saved.", ("Stopped", "neutral", "stop_square"), (None, {}), need=2),
        "waiting": Portal("waiting", now - 60, None, None, "2 changes wait for your answer", "From Claude (Claude Code)",
                          badge=("Waiting for you", "warn", "person"), expires=now + 570, need=2, page="approvals"),
        "blocked": Portal("blocked", now - 30, claude, None, "A QuickBooks window is open and waiting for someone", "Close it, then retry.",
                          badge=("Blocked by a window", "bad", "window"), page="problems"),
        "closed": Portal("closed", now - 30, claude, None, "QuickBooks is closed", "Claude (Claude Code) can't reach the books.",
                         badge=("Problem", "bad", "x"), page="problems"),
        "timeout": Portal("timeout", now - 180, gemini, None, "Still running inside QuickBooks", "Who owes money gave up after 2 min.",
                          badge=("Timed out", "bad", "clock"), line=1),
        "in line": Portal("line", now - 12, gemini, cmd(gemini, "Bills to pay", now - 12), "Bills to pay",
                          "Gemini (agy) · waiting for its turn", badge=("In line", "warn", "clock"), line=1),
        "done": Portal("done", now - 4, gemini, None, "Cash position", "Gemini (agy) · 12 records · 0.7 s", badge=("Done", "ok", "check_circle")),
        "idle": Portal("idle"),
    }


@unittest.skipUnless(wx is not None and SCREEN, "needs wxPython and a screen")
class Base(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        if wx.GetApp() is None:
            Base.app = wx.App(False)
        W.SCALE = 1.0

    def setUp(self):
        self.errors: list = []
        hook = sys.excepthook
        sys.excepthook = lambda *e: self.errors.append(e)
        self.addCleanup(self.no_errors, hook)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = design_now()
        self.clock = [self.now]
        state = State(None)
        state.data["problems_seen"] = design_seen(self.now)
        feed = Feed(ENV, self.now - KEEP_DAYS * 86400, only=[write_sample(folder.name, self.now)], sample=True)
        self.model = Model(feed, state, settings=lambda: dict(ENV), clock=lambda: self.clock[0], tools=COMMANDS)

    def no_errors(self, hook) -> None:
        sys.excepthook = hook
        if self.errors:
            import traceback

            self.fail("".join(traceback.format_exception(*self.errors[0])))

    def controller(self, dark: bool = False) -> Controller:
        app = Controller(self.model, tray=False, dark=dark, single=False)
        self.addCleanup(self.close, app)
        return app

    def close(self, app: Controller) -> None:
        for frame in (app.portal, app.flyout, app.window):
            frame.Destroy()
        pump()


class Window(Base):
    def test_every_page_lays_out_wide_and_narrow_in_light_and_dark(self):
        free = wx.Display(0).GetClientArea()
        wide = min(1280, free.width - 40)  # a window gets no wider than its screen: a CI runner's is 1024 wide
        if wide < NARROW:
            with self.subTest(width=wide):
                self.skipTest(f"a screen {free.width} wide has no room for the wide layout")
        for dark in (False, True):
            app = self.controller(dark)
            win = app.window
            win.Show()
            for width in (wide, 600) if wide >= NARROW else (600,):
                win.SetClientSize(width, 900)
                pump()
                for name, page in win.pages.items():
                    with self.subTest(dark=dark, width=width, page=name):
                        if name == "session":
                            app.open_session(app.model.overview(app.pick).sessions[0].sid)
                        else:
                            app.show(name)
                        pump()
                        win.Update()
                        self.assertIs(win.current(), page)
                        self.assertEqual(app.narrow, width < NARROW)
                        self.assertGreater(page.scroll.GetVirtualSize()[1], 300)
                        if page.title:  # narrow, it is in the window's bar
                            self.assertIn(page.title, words(page.col) + [win.top_title.text()])

    def test_the_tray_panel_opens_inside_the_screen_and_shows_what_runs_and_waits(self):
        app = self.controller()
        free = wx.Display(0).GetClientArea()  # where it opens from a point on no screen
        if free.height < app.flyout.GetSize().height + 16:  # below the 800×600 Windows 10 needs
            self.skipTest(f"a screen's free area {free.height} high has no room for the panel")
        app.flyout.show_near(wx.Point(10 ** 5, 10 ** 5))  # past the screen's corner, where a tray icon may be
        pump()
        self.assertTrue(app.flyout.IsShown())
        self.assertTrue(free.Contains(app.flyout.GetScreenRect()))
        shown = words(app.flyout.root.node)
        running, _ = app.model.live(ALL)
        self.assertIn(running.title, shown)
        self.assertIn("Open activity", shown)
        esc = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
        esc.SetKeyCode(wx.WXK_ESCAPE)
        app.flyout.on_key(esc)
        self.assertFalse(app.flyout.IsShown())

    def test_enter_opens_a_line_of_the_tray_panel_and_a_change(self):
        # A node that has a name to be found by (its key) lost its keyboard handler to that name: Enter on a line of the
        # panel, a change, its Undo or a list's period raised TypeError instead of opening it.
        app = self.controller()
        enter = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
        enter.SetKeyCode(wx.WXK_RETURN)
        app.flyout.show_near(wx.Point(10 ** 5, 10 ** 5))
        pump()
        line = next(n for n in app.flyout.lines.walk() if isinstance(n, W.Row))
        line.win.on_key(enter)
        pump()
        self.assertFalse(app.flyout.IsShown())
        self.assertTrue(app.window.IsShown())
        self.assertEqual(app.page, line.key.split(":")[1])
        app.show("changes")
        pump()
        page = app.window.pages["changes"]
        head = next(n for n in page.col.walk() if isinstance(n, W.Row) and n.description == "collapsed")
        head.win.on_key(enter)
        pump()
        self.assertIn(head.key, page.opened)

    def test_a_section_built_again_hides_what_it_drops_before_destroying_it(self):
        # Windows fills the place of each child destroyed before its parent with the system grey, and the parent shows
        # those holes until the new windows paint: the Live page flashed grey each second while a request ran.
        app = self.controller()
        app.show("live")
        pump()
        shown = []
        destroy = wx.Window.Destroy

        def watched(win):
            shown.append(win.IsShown())
            return destroy(win)

        with mock.patch.object(W.Canvas, "Destroy", watched):
            app.window.pages["live"].middle.show_built(object(), lambda: W.Text("Built again"))
            pump()
        self.assertTrue(shown)
        self.assertNotIn(True, shown)

    def test_a_node_window_has_its_paint_style_before_it_exists(self):
        # Made in one step, a window was erased with Windows grey as it was born, before its paint style was set: grey
        # squares at the cards' corners of the Live page each second while a request ran.
        styles = []
        create = wx.Control.Create

        def watched(win, *args, **kw):
            styles.append(win.GetBackgroundStyle())
            return create(win, *args, **kw)

        frame = wx.Frame(None)
        self.addCleanup(frame.Destroy)
        with mock.patch.object(W.Canvas, "Create", watched):
            W.Text("Born").realize(frame)
        self.assertEqual(styles, [wx.BG_STYLE_PAINT])

    def test_a_click_on_the_tray_icon_opens_the_panel_once_a_double_click_can_no_longer_come(self):
        # Opened at once, the panel took the focus, which closes Windows' hidden icons, and the second click of a double
        # click there landed on the window under them: the window never opened.
        app = self.controller()
        tray = Tray(app)  # only its handlers run: it shows no icon
        self.addCleanup(tray.Destroy)
        later = double_click_ms() + 300
        with mock.patch.object(app.flyout, "show_near") as show_near:
            tray.on_click(None)
            pump()
            show_near.assert_not_called()
            wait(later)
            show_near.assert_called_once()
            show_near.reset_mock()
            for handle in (tray.on_click, tray.on_double_click, tray.on_click):  # what Windows sends for a double click
                handle(None)
            wait(later)
            show_near.assert_not_called()
        self.assertTrue(app.window.IsShown())

    def test_quit_lets_wx_delete_the_windows_before_its_loop_ends(self):
        # A window wx still holds then is deleted as Python exits, which crashed Python on Windows (wx 4.3).
        app = Controller(self.model, tray=False, dark=False, single=False)

        def close_left() -> None:  # what a failure leaves, so the next tests start without it
            for w in wx.GetTopLevelWindows():
                w.Destroy()
            pump()

        self.addCleanup(close_left)
        app.flyout.show_near(wx.Point(10 ** 5, 10 ** 5))
        wx.CallLater(100, app.quit)  # from inside the loop, as the tray's Quit and a picture call it
        wx.GetApp().MainLoop()
        self.assertEqual([type(w).__name__ for w in wx.GetTopLevelWindows()], [])


class PortalStates(Base):
    def test_each_state_of_the_portal_board_lays_out_light_and_dark(self):
        for dark in (False, True):
            app = self.controller(dark)
            p = app.portal
            for name, v in views(self.now).items():
                with self.subTest(dark=dark, state=name):
                    app.portal_view = v
                    p.open(activate=False)
                    pump()
                    p.Update()
                    self.assertEqual(p.GetSize()[0], WIDTH)
                    self.assertGreater(p.GetSize()[1], 40)
                    self.assertEqual(p.expand.name, f"Expand: open the window on {PAGES.get(v.page, 'Overview')}")
                    if v.title:
                        self.assertIn(v.title, "\n".join(words(p.root.node)))
                    p.Hide()

    def shown(self, app: Controller, name: str, by_itself: bool = False) -> list[str]:
        app.portal_view = views(self.now)[name]
        app.by_itself = by_itself
        app.portal.open(activate=False)
        pump()
        return words(app.portal.root.node)

    def test_what_each_state_offers(self):
        app = self.controller()
        self.assertIn("Stop after this step", self.shown(app, "running"))
        self.assertIn("2 need you", self.shown(app, "running"))
        stopping = self.shown(app, "stopping")
        self.assertIn("Stopping after this step. Claude stops when this page is read, then waits until you resume.", stopping)
        self.assertNotIn("Stop after this step", stopping)
        self.assertIn("Resume Claude", self.shown(app, "stopped"))
        waiting = self.shown(app, "waiting")
        self.assertIn("Review", waiting)
        self.assertNotIn("2 need you", waiting)  # the state says it already
        self.assertIn("See the problem", self.shown(app, "blocked"))
        self.assertIn("Still running inside QuickBooks · for 3 min", self.shown(app, "timeout"))
        self.assertIn(f"waited 12 s of {LINE_WAIT} s", self.shown(app, "in line"))
        self.assertIn(f"Hides in {HIDE_AFTER - 4} s unless another request starts.", self.shown(app, "done", by_itself=True))
        self.assertNotIn(f"Hides in {HIDE_AFTER - 4} s unless another request starts.", self.shown(app, "done"))


class PortalRules(Base):
    """When it shows and hides, and where it sits (the Portal board)."""

    def test_it_opens_by_itself_when_a_request_starts_and_the_window_is_away(self):
        app = self.controller()  # the window is not on screen until the monitor starts
        app.follow()
        self.assertTrue(app.portal.IsShown())
        self.assertTrue(app.by_itself)
        self.assertEqual(app.portal.view.state, "running")

    def test_not_while_the_window_is_on_screen_nor_with_the_automatic_portal_off(self):
        app = self.controller()
        app.show("overview")
        app.follow()
        self.assertFalse(app.portal.IsShown())
        app.window.Hide()
        app.set_portal_auto(False)
        app.running_pids = set()
        app.follow()
        self.assertFalse(app.portal.IsShown())
        self.assertFalse(app.model.state.data["portal"]["auto"])

    def test_closed_it_stays_away_until_another_request_starts(self):
        app = self.controller()
        app.follow()
        app.close_portal()
        app.follow()
        self.assertFalse(app.portal.IsShown())
        running, waiting = app.model.live(ALL)
        later = cmd(Who(*CLAUDE), "Bills to pay", self.now, pid=running.pid + 1)
        with mock.patch.object(app.model, "live", return_value=(later, waiting)):
            app.follow()
        self.assertTrue(app.portal.IsShown())

    def test_opened_by_itself_it_hides_30_s_after_the_last_request_ends(self):
        app = self.controller()
        done = views(self.now)["done"]  # ended 4 s ago
        last = types.SimpleNamespace(src="calls.jsonl", line=10 ** 6, e={}, t=done.since)
        with mock.patch.object(app.model, "live", return_value=(None, [])), \
                mock.patch.object(app.model, "portal", return_value=done), \
                mock.patch.object(app.model, "last_request", return_value=last):
            app.follow()  # a request too short to be seen running still opens it
            self.assertTrue(app.portal.IsShown())
            self.clock[0] = done.since + HIDE_AFTER - 1
            app.follow()
            self.assertTrue(app.portal.IsShown())
            self.clock[0] = done.since + HIDE_AFTER
            app.follow()
            self.assertFalse(app.portal.IsShown())
            app.show_portal()  # opened by hand, it stays
            self.clock[0] += 600
            app.follow()
            self.assertTrue(app.portal.IsShown())

    def test_expand_opens_the_window_on_the_page_that_fits_and_the_window_hides_the_portal(self):
        app = self.controller()
        app.follow()
        app.expand_portal()
        self.assertFalse(app.portal.IsShown())
        self.assertTrue(app.window.IsShown())
        self.assertEqual(app.page, "live")
        with mock.patch.object(app.model, "portal", return_value=views(self.now)["waiting"]):
            app.show_portal()
            self.assertTrue(app.portal.IsShown())
            app.expand_portal()
        self.assertEqual(app.page, "approvals")
        app.show_portal()
        app.show("problems")
        self.assertFalse(app.portal.IsShown())

    def test_dragged_it_settles_in_the_nearest_corner_and_keeps_it_for_its_screen(self):
        app = self.controller()
        app.follow()
        p = app.portal
        free = wx.Display(max(0, wx.Display.GetFromWindow(p))).GetClientArea()
        start = p.GetPosition()
        self.assertEqual(p.GetRect().GetBottomRight(), free.GetBottomRight() - wx.Point(MARGIN, MARGIN))  # first at the bottom right
        p.drag("down", start + wx.Point(10, 10))
        p.drag("move", wx.Point(free.x + 60, free.y + 60))
        p.drag("up", wx.Point(free.x + 60, free.y + 60))
        self.assertEqual(p.corner, "tl")
        self.assertEqual(p.GetPosition(), wx.Point(free.x + MARGIN, free.y + MARGIN))
        self.assertEqual(app.portal_settings()["corners"], {p.display: "tl"})
        p.nudge(wx.WXK_RIGHT)  # the arrow keys on its grip
        self.assertEqual(p.corner, "tr")
        app.close_portal()
        app.show_portal()
        self.assertEqual(p.corner, "tr")
        self.assertEqual(p.GetRect().GetTopRight(), wx.Point(free.GetRight() - MARGIN, free.y + MARGIN))


if __name__ == "__main__":
    unittest.main()
