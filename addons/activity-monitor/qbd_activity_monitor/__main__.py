"""qbd addons run activity-monitor (or python -m qbd_activity_monitor): starts the Activity Monitor."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from typing import Any


def dpi_aware() -> None:
    """Sharp text on a scaled display: the monitor draws itself at the system's scale, and Windows stretches it on a
    screen with another scale."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE, Windows 8.1 and later
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="activity-monitor", description="The Activity Monitor: what AI assistants do in QuickBooks through qbd.")
    p.add_argument("--tray", action="store_true", help="start in the tray without the window, as Windows starts it")
    p.add_argument("--state-dir", action="append", default=[], metavar="FOLDER", help="also read this qbd state folder")
    p.add_argument("--sample", nargs="?", const="", metavar="FOLDER",
                   help="show the monitor's sample data instead of this PC's, written to FOLDER (default: one in the temp folder)")
    p.add_argument("--design-time", action="store_true", help="with --sample: the design's clock, 21 Oct 2026 10:04:12")
    p.add_argument("--theme", choices=("light", "dark"), help="instead of Windows' setting")
    p.add_argument("--screenshot", metavar="PNG", help="save a picture of one page and stop")
    p.add_argument("--page", default="overview", help="with --screenshot: overview, live, sessions, session:N[:R], approvals, "
                   "changes, problems, safety, tray or portal")
    p.add_argument("--size", default="1280x900", help="with --screenshot: the window's size, WxH")
    args = p.parse_args(argv)
    dpi_aware()
    try:
        import wx
    except ImportError:
        print("The Activity Monitor's wxPython is missing; install the add-on again: qbd addons install activity-monitor", file=sys.stderr)
        return 2

    from qbd_agent_kit.api import COMMANDS, state_dir

    from .feed import KEEP_DAYS, Feed, pc_env
    from .model import Model, State
    from .sample import ENV, design_now, design_seen, write_sample
    from .ui import widgets
    from .ui.app import Controller, another_running

    wxapp = wx.App(False)
    wxapp.SetAppName("Activity Monitor")
    widgets.SCALE = max(1.0, wx.ScreenDC().GetPPI()[0] / 96)
    single = not args.screenshot and args.sample is None
    if single and another_running():
        return 0
    if args.sample is not None:
        now = design_now() if args.design_time else time.time()
        folder = args.sample or os.path.join(tempfile.gettempdir(), "qbd-monitor-sample")
        write_sample(folder, now)
        clock = (lambda: now) if args.design_time else time.time
        state = State(None)
        state.data["problems_seen"] = design_seen(now)
        feed = Feed(ENV, now - KEEP_DAYS * 86400, only=[folder], sample=True)
        model = Model(feed, state, settings=lambda: dict(ENV), clock=clock, tools=COMMANDS)
    else:
        env = pc_env()
        own = state_dir({k: v for k, v in env.items() if k != "QBD_STATE_DIR"})
        state = State(os.path.join(own, "monitor.json"))
        feed = Feed(env, time.time() - KEEP_DAYS * 86400, extra=args.state_dir, marks=state.data.get("marks"))
        model = Model(feed, state, settings=pc_env, tools=COMMANDS)
    dark = {"light": False, "dark": True}.get(args.theme or "")
    app = Controller(model, tray=not args.screenshot, dark=dark, single=single)
    if args.screenshot:
        return screenshot(wxapp, app, args.page, args.size, args.screenshot)
    app.start(show_window=not args.tray)
    wxapp.MainLoop()
    return 0


def screenshot(wxapp: Any, app: Any, page: str, size: str, out: str) -> int:
    """A picture of one page at one size, for the design check; session:N[:R] opens today's Nth session (from 0) with
    its Rth request and that request's technical details open."""
    import wx

    width, height = (int(x) for x in size.lower().split("x"))
    win = app.window
    name, _, rest = page.partition(":")
    if name == "tray":
        target = app.flyout
        app.flyout.show_near(wx.Point(10 ** 5, 10 ** 5))
    elif name == "portal":
        target = app.portal
        app.follow()
        app.show_portal()
    else:
        target = win
        win.SetWindowStyle(win.GetWindowStyle() | wx.STAY_ON_TOP)  # over a window in front, such as QuickBooks
        win.SetClientSize(width, height)
        win.Move(0, 0)  # the whole window on the screen, where the picture is taken from
        if name == "session":
            index, _, request = rest.partition(":")
            s = app.model.overview(app.pick).sessions[int(index or 0)]
            pg = win.pages["session"]
            pg.sid = s.sid
            detail = app.model.session(s.sid)
            if request and detail is not None:
                q = detail.requests[int(request) - 1]
                pg.opened |= {f"request:{q.rec.src}:{q.rec.line}", f"tech:{q.rec.src}:{q.rec.line}"}
            app.show("session")
        else:
            app.show(name)
    saved: list[bool] = []

    def take() -> None:
        app.refresh()
        wx.SafeYield()
        x, y = target.ClientToScreen(wx.Point(0, 0))
        w, h = target.GetClientSize()
        bmp = wx.Bitmap(w, h)
        dc = wx.MemoryDC(bmp)
        dc.Blit(0, 0, w, h, wx.ScreenDC(), x, y)
        dc.SelectObject(wx.NullBitmap)
        saved.append(bmp.SaveFile(out, wx.BITMAP_TYPE_PNG))
        print(out if saved[0] else f"could not save {out}")
        app.quit()

    wx.CallLater(1200, take)
    wxapp.MainLoop()
    return 0 if saved and saved[0] else 1


if __name__ == "__main__":
    sys.exit(main())
