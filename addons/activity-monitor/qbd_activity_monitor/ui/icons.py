"""Line icons in the Fluent style, drawn from small SVGs in any colour. Icons next to a status word are hidden from
screen readers: the word says it."""
from __future__ import annotations

from functools import lru_cache

import wx

_S = 'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"'
PATHS = {
    "home": f'<path {_S} d="M2.5 7.2 8 2.8l5.5 4.4V13a.6.6 0 0 1-.6.6H10V9.6H6v4H3.1a.6.6 0 0 1-.6-.6Z"/>',
    "running": f'<path {_S} d="M1.5 8.5h2.6L6 4l2.6 8.2 2-5.2 1 1.5h2.9"/>',
    "list": f'<path {_S} d="M5.5 4h8M5.5 8h8M5.5 12h8"/><circle cx="2.6" cy="4" r=".8" fill="currentColor"/>'
            '<circle cx="2.6" cy="8" r=".8" fill="currentColor"/><circle cx="2.6" cy="12" r=".8" fill="currentColor"/>',
    "approval": f'<rect {_S} x="2.5" y="2.5" width="11" height="11" rx="2"/><path {_S} d="m5.3 8.2 1.8 1.8 3.6-3.8"/>',
    "history": f'<path {_S} d="M2.6 8a5.4 5.4 0 1 0 1.6-3.8"/><path {_S} d="M2.4 2.6v2.2h2.2"/><path {_S} d="M8 5.2V8l1.9 1.3"/>',
    "warning": f'<path {_S} d="M7.1 2.6a1 1 0 0 1 1.8 0l5.3 9.6a1 1 0 0 1-.9 1.5H2.7a1 1 0 0 1-.9-1.5Z"/><path {_S} d="M8 6.2v3.1"/>'
               '<circle cx="8" cy="11.3" r=".75" fill="currentColor"/>',
    "shield": f'<path {_S} d="M8 1.9c1.7 1.2 3.4 1.8 5.3 1.9v3.6c0 3.2-2.1 5.5-5.3 6.7-3.2-1.2-5.3-3.5-5.3-6.7V3.8C4.6 3.7 6.3 3.1 8 1.9Z"/>',
    "file": f'<path {_S} d="M4.3 1.9h4.6l3.3 3.3v8.3a.7.7 0 0 1-.7.7H4.3a.7.7 0 0 1-.7-.7V2.6a.7.7 0 0 1 .7-.7Z"/><path {_S} d="M8.8 2v3.3h3.3"/>',
    "chevron_down": f'<path {_S} d="m4.5 6.3 3.5 3.5 3.5-3.5"/>',
    "chevron_up": f'<path {_S} d="m4.5 9.7 3.5-3.5 3.5 3.5"/>',
    "chevron_right": f'<path {_S} d="m6.3 4.5 3.5 3.5-3.5 3.5"/>',
    "clock": f'<circle {_S} cx="8" cy="8" r="5.6"/><path {_S} d="M8 4.9V8l2 1.3"/>',
    "person": f'<circle {_S} cx="8" cy="5.4" r="2.5"/><path {_S} d="M3.4 13.4c.5-2.4 2.3-3.7 4.6-3.7s4.1 1.3 4.6 3.7"/>',
    "check": f'<path {_S} d="m3.2 8.4 3.1 3 6.5-6.8"/>',
    "check_circle": f'<circle {_S} cx="8" cy="8" r="5.6"/><path {_S} d="m5.4 8.2 1.8 1.8 3.5-3.7"/>',
    "x": f'<circle {_S} cx="8" cy="8" r="5.6"/><path {_S} d="m6 6 4 4m0-4-4 4"/>',
    "x_plain": f'<path {_S} d="m4 4 8 8m0-8-8 8"/>',
    "slash": f'<circle {_S} cx="8" cy="8" r="5.6"/><path {_S} d="m4.1 11.9 7.8-7.8"/>',
    "play": '<path fill="currentColor" d="M5 3.4v9.2a.5.5 0 0 0 .8.4l7-4.6a.5.5 0 0 0 0-.8l-7-4.6a.5.5 0 0 0-.8.4Z"/>',
    "pause": f'<path {_S} d="M5.8 3.8v8.4M10.2 3.8v8.4"/>',
    "info": f'<circle {_S} cx="8" cy="8" r="5.6"/><path {_S} d="M8 7.3v3.6"/><circle cx="8" cy="5.2" r=".75" fill="currentColor"/>',
    "window": f'<rect {_S} x="2.2" y="3" width="11.6" height="10" rx="1.4"/><path {_S} d="M2.4 6h11.2"/>',
    "search": f'<circle {_S} cx="6.8" cy="6.8" r="4.3"/><path {_S} d="m10 10 3.6 3.6"/>',
    "undo": f'<path {_S} d="M5.6 4.1 2.8 6.8l2.8 2.7"/><path {_S} d="M3 6.8h6.4a3.6 3.6 0 0 1 0 7.2H7"/>',
    "copy": f'<rect {_S} x="5.3" y="5.3" width="8.2" height="8.2" rx="1.3"/><path {_S} d="M10.7 5.2V3.6a1.1 1.1 0 0 0-1.1-1.1H3.6a1.1 1.1 0 0 0-1.1 1.1v6a1.1 1.1 0 0 0 1.1 1.1h1.6"/>',
    "menu": f'<path {_S} d="M2.5 4.2h11M2.5 8h11M2.5 11.8h11"/>',
    "moon": f'<path {_S} d="M12.9 9.9A5.4 5.4 0 0 1 6.1 3.1a5.4 5.4 0 1 0 6.8 6.8Z"/>',
    "deposit": f'<path {_S} d="M8 2.3v6.8m0 0L5.4 6.5M8 9.1l2.6-2.6"/><path {_S} d="M2.6 9.6v2.6a1.2 1.2 0 0 0 1.2 1.2h8.4a1.2 1.2 0 0 0 1.2-1.2V9.6"/>',
    "receipt": f'<path {_S} d="M4 1.9h8v12.2l-1.6-1-1.2 1-1.2-1-1.2 1-1.2-1-1.6 1Z"/><path {_S} d="M6 5.3h4M6 7.8h4M6 10.3h2.4"/>',
    "document": f'<path {_S} d="M4.3 1.9h4.6l3.3 3.3v8.3a.7.7 0 0 1-.7.7H4.3a.7.7 0 0 1-.7-.7V2.6a.7.7 0 0 1 .7-.7Z"/><path {_S} d="M6 8.5h4M6 11h4"/>',
    "external": f'<path {_S} d="M9.5 2.5h4v4M13.5 2.5 7.8 8.2"/><path {_S} d="M11.8 9.5v3.3a.8.8 0 0 1-.8.8H3.2a.8.8 0 0 1-.8-.8V5a.8.8 0 0 1 .8-.8h3.3"/>',
    "dot": '<circle cx="8" cy="8" r="3" fill="currentColor"/>',
}
# The portal's icons, drawn on a 20 px grid as the Portal board has them.
_P = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"'
PATHS20 = {
    "grip": "".join(f'<circle cx="{x}" cy="{y}" r="1.25" fill="currentColor"/>' for y in (5.5, 10, 14.5) for x in (7.5, 12.5)),
    "expand": f'<path {_P} stroke-width="1.5" d="M11.5 3.5h5v5m0-5-5.25 5.25M8.5 16.5h-5v-5m0 5 5.25-5.25"/>',
    "close": f'<path {_P} stroke-width="1.5" d="m5.5 5.5 9 9m0-9-9 9"/>',
    "stop_square": f'<circle {_P} stroke-width="1.5" cx="10" cy="10" r="7"/><rect {_P} stroke-width="1.5" x="7.5" y="7.5" width="5" height="5" rx="1"/>',
    "line": f'<circle {_P} stroke-width="1.6" cx="10" cy="10" r="7"/><path {_P} stroke-width="1.6" d="M10 6.5V10l2.5 1.5"/>',
    "need": f'<rect {_P} stroke-width="1.6" x="3.5" y="3.5" width="13" height="13" rx="2.5"/><path {_P} stroke-width="1.6" d="m7 10.2 2.1 2.1L13.2 8"/>',
    "resume": '<path fill="currentColor" d="M6.5 4.75v10.5L15 10z"/>',
    "pulse": f'<path {_P} stroke-width="1.8" d="M2.5 10h3l2-5 3.5 10 2-5h4.5"/>',
    "portal": f'<rect {_P} stroke-width="1.2" x="2.5" y="4.5" width="15" height="11" rx="1.5"/>'
              '<rect fill="currentColor" x="10" y="10" width="5.5" height="3.5" rx=".75"/>',
}


def svg(name: str, colour: str) -> bytes:
    if name in PATHS20:
        body, box = PATHS20[name], 20
    else:
        body, box = PATHS.get(name, PATHS["info"]), 16
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {box} {box}">{body.replace("currentColor", colour)}</svg>'.encode()


@lru_cache(maxsize=1024)
def bitmap(name: str, colour: str, size: int) -> wx.Bitmap:
    """The icon at size pixels in a colour."""
    return wx.BitmapBundle.FromSVG(svg(name, colour), wx.Size(size, size)).GetBitmap(wx.Size(size, size))


def tray_icon(base: wx.Image, state: str, colours: dict[str, str]) -> wx.BitmapBundle:
    """The kit's icon with the state's mark: amber square for waiting for you, red triangle for a problem, blue dot
    for working, none when idle. Each mark has its own shape, so the state never rests on colour alone."""
    sizes = []
    for size in (16, 20, 24, 32, 48):
        image = base.Scale(size, size, wx.IMAGE_QUALITY_HIGH)
        if not image.HasAlpha():
            image.InitAlpha()
        if state != "idle":
            gc = wx.GraphicsContext.Create(image)
            mark = size * 0.56
            x = y = size - mark
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(wx.Colour("#FFFFFF")))
            gc.DrawEllipse(x - 1, y - 1, mark + 2, mark + 2)
            gc.SetBrush(wx.Brush(wx.Colour({"waiting": colours["warn_mark"], "problem": colours["bad_mark"],
                                            "working": colours["info_mark"]}[state])))
            if state == "problem":
                path = gc.CreatePath()
                path.MoveToPoint(x + mark / 2, y)
                path.AddLineToPoint(x + mark, y + mark)
                path.AddLineToPoint(x, y + mark)
                path.CloseSubpath()
                gc.FillPath(path)
            elif state == "waiting":
                gc.DrawRoundedRectangle(x, y, mark, mark, mark * 0.3)
            else:
                gc.DrawEllipse(x, y, mark, mark)
            del gc  # writes the drawing into the image
        sizes.append(wx.Bitmap(image))
    return wx.BitmapBundle.FromBitmaps(sizes)
