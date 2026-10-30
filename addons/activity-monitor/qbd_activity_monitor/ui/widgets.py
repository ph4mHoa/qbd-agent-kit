"""The building blocks of every screen, from the design's Component sheet: text in the type ramp, one-line text with
an ellipsis, status badges, assistant avatars, progress bars, buttons, cards, tiles, banners, rows and drop-down
lists. Every control works by keyboard, shows its focus, and gives screen readers a name; a status always has its
word.

A screen is a tree of nodes. Boxes and grids only place what they hold, the way the design's pages do: a line of text
wraps to the width it gets, a name ends in an ellipsis, a column keeps its width. The rest are small windows that
paint themselves over what lies behind them, so rounded cards and hover colours look the same on every Windows. Each
root (a page, the navigation pane, the tray panel, the portal) lays its tree out again when something in it changed
size.

Text from assistants and from QuickBooks (titles, names, summaries, errors) is data: it is painted as plain text,
never read as markup, so it cannot draw links, pictures or look-alike controls."""
from __future__ import annotations

import math
import re
import sys
import weakref
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterator

import wx

from . import icons
from .theme import TYPE, Theme, colour, font

THEME = Theme(dark=False)
SCALE = 1.0            # pixels per design pixel: Windows' display scale
REDUCED_MOTION = False
BIG = 10 ** 6


def set_theme(t: Theme) -> None:
    global THEME
    THEME = t


def px(n: float) -> int:
    return int(round(n * SCALE))


def c(key: str) -> wx.Colour:
    return colour(THEME.c[key])


def tone_hex(tone: str) -> str:
    k = THEME.c
    return {"secondary": k["secondary"], "accent": k["accent"], "ok": k["ok"], "warn": k["warn"], "bad": k["bad"], "info": k["info"],
            "disabled": k["disabled"], "neutral": k["neutral"], "on_accent": k["on_accent"]}.get(tone, k["text"])


def menu_text(text: str) -> str:
    """Text for a native menu, where & would mark a shortcut key."""
    return text.replace("&", "&&")


# ---------------------------------------------------------------- measuring text

_DC: wx.MemoryDC | None = None


def _dc() -> wx.MemoryDC:
    global _DC
    if _DC is None:
        _DC = wx.MemoryDC(wx.Bitmap(1, 1))
    return _DC


@lru_cache(maxsize=50000)
def _width(text: str, role: str, scale: float) -> int:
    dc = _dc()
    dc.SetFont(font(role, scale))
    return dc.GetTextExtent(text)[0]


@lru_cache(maxsize=64)
def _height(role: str, scale: float) -> int:
    dc = _dc()
    dc.SetFont(font(role, scale))
    return dc.GetTextExtent("Ág")[1]


def text_w(text: str, role: str = "body") -> int:
    return _width(text, role, SCALE)


def line_h(role: str) -> int:
    return max(px(TYPE.get(role, TYPE["body"])[2]), _height(role, SCALE))


def _extents(text: str, role: str) -> list[int]:
    dc = _dc()
    dc.SetFont(font(role, SCALE))
    return list(dc.GetPartialTextExtents(text)) if text else []


def elide(text: str, role: str, width: int) -> str:
    """The text cut to width with an ellipsis, or whole when it fits."""
    if text_w(text, role) <= width:
        return text
    dots = text_w("…", role)
    ext = _extents(text, role)
    n = 0
    while n < len(text) and ext[n] + dots <= width:
        n += 1
    return text[:n].rstrip() + "…" if n else ("…" if dots <= width else "")


TOKEN = re.compile(r"\n|[^\s/\\]*[/\\]+|\S+[^\S\n]*|[^\S\n]+")  # spaces after a word, never a line's end


def _tokens(text: str) -> list[str]:
    """Words with their trailing spaces; a path breaks after each / or \\ as well."""
    return TOKEN.findall(text)


@dataclass
class Run:
    text: str
    role: str = "body"
    tone: str = ""


def wrap(runs: list[Run], width: int) -> list[list[tuple[int, str, Run]]]:
    """Lines of (x, text, run) that fit width: breaking at spaces, after / and \\, and inside a word too long for a
    line."""
    lines: list[list[tuple[int, str, Run]]] = [[]]
    x = 0

    def put(text: str, run: Run, w: int) -> None:
        nonlocal x
        line = lines[-1]
        if line and line[-1][2] is run and line[-1][0] + text_w(line[-1][1], run.role) == x:
            line[-1] = (line[-1][0], line[-1][1] + text, run)
        else:
            line.append((x, text, run))
        x += w

    for run in runs:
        for tok in _tokens(run.text):
            if tok == "\n":
                lines.append([])
                x = 0
                continue
            full, trim = text_w(tok, run.role), text_w(tok.rstrip(), run.role)
            if x and x + trim > width:
                lines.append([])
                x = 0
                if tok.isspace():
                    continue
            if trim > width:  # a word longer than the line: cut it where it must
                ext = _extents(tok, run.role)
                start, base = 0, 0
                for i in range(len(tok)):
                    if ext[i] - base > width - x and i > start:
                        put(tok[start:i], run, ext[i - 1] - base)
                        lines.append([])
                        x = 0
                        start, base = i, ext[i - 1]
                put(tok[start:], run, full - base)
            else:
                put(tok, run, full)
    return [[(x0, t.rstrip() if i == len(line) - 1 else t, r) for i, (x0, t, r) in enumerate(line)] for line in lines]


# ---------------------------------------------------------------- the layout tree


class Node:
    """Something on a screen: it says how wide it can be and how tall it is at a width, and places itself."""

    grow = 0          # its share of the room left in a box
    align = ""        # in a row: top, bottom, fill (default centre); in a column: left, centre, right (default fill)
    hidden = False
    parent: "Node | None" = None
    fill_x = True     # in a column, by default it takes the whole width
    max_w = BIG
    fixed_w: int | None = None   # a column's width in a list, in pixels: line_up gives one width down the list
    least_w = 0       # the narrowest it may get, in pixels, whatever its text would allow
    key = ""

    def min_w(self) -> int:
        return 0

    def pref_w(self) -> int:
        return 0

    def height(self, w: int) -> int:
        return 0

    def realize(self, host: wx.Window) -> None:
        """Makes its windows (and its children's) in host."""

    def place(self, x: int, y: int, w: int, h: int) -> None:
        """Puts itself at x, y in its host's coordinates."""

    def kill(self) -> None:
        """Forgets its windows; the topmost window of what is dropped destroys the rest."""

    def walk(self) -> Iterator["Node"]:
        yield self

    def sync_shown(self, visible: bool) -> None:
        """Shows or hides its windows as it and its parents are hidden or not."""

    def show(self, on: bool) -> None:
        if self.hidden == (not on):
            return
        self.hidden = not on
        self.sync_shown(not self.own_hidden())
        self.request_layout()

    def own_hidden(self) -> bool:
        """Hidden itself, or by a box between it and the window it lies in (a hidden window hides what it holds)."""
        n: Node | None = self
        while n is not None:
            if n.hidden:
                return True
            n = n.parent
            if isinstance(n, Win):
                return False
        return False

    def top(self) -> "Node":
        n = self
        while n.parent is not None:
            n = n.parent
        return n

    def request_layout(self) -> None:
        root = getattr(self.top(), "root", None)
        if root is not None:
            root.schedule()


def least(n: Node) -> int:
    """The narrowest a node can be."""
    return n.fixed_w if n.fixed_w is not None else max(n.min_w(), n.least_w)


def best(n: Node) -> int:
    """The width a node asks for."""
    return n.fixed_w if n.fixed_w is not None else max(min(n.pref_w(), n.max_w), n.least_w)


def fixed(n: Node, width: int) -> Node:
    """n with a width of its own, in pixels: a column of a list."""
    n.fixed_w = width
    return n


class Spacer(Node):
    def __init__(self, size: int = 0, grow: int = 0):
        self.size, self.grow = size, grow


class Box(Node):
    """A row or a column of nodes, with padding around and a gap between them."""

    def __init__(self, vertical: bool = True, pad: tuple = (0, 0, 0, 0), gap: int = 0):
        self.vertical, self.pad, self.gap = vertical, pad, gap
        self.items: list[Node] = []

    # building
    def add(self, node: "Node | None", grow: int = 0, align: str = "") -> Any:
        if node is None:
            return None
        node.grow, node.align, node.parent = grow, align or node.align, self
        self.items.append(node)
        return node

    def insert(self, i: int, node: Node, grow: int = 0, align: str = "") -> Node:
        node.grow, node.align, node.parent = grow, align or node.align, self
        self.items.insert(i, node)
        return node

    def remove(self, node: Node) -> None:
        self.items.remove(node)

    def space(self, n: int) -> None:
        self.add(Spacer(n))

    def spring(self, n: int = 1) -> None:
        self.add(Spacer(0, n), n)

    # measuring
    def shown(self) -> list[Node]:
        return [i for i in self.items if not i.hidden]

    def _pads(self) -> tuple[int, int, int, int]:
        return tuple(px(p) for p in self.pad)  # type: ignore[return-value]

    def _size(self, n: Node) -> int:
        return px(n.size) if isinstance(n, Spacer) else 0

    def min_w(self) -> int:
        items = self.shown()
        pl, _, pr, _ = self._pads()
        if self.vertical:
            return pl + pr + max([0] + [least(i) for i in items])
        return pl + pr + px(self.gap) * max(0, len(items) - 1) + sum(self._size(i) or least(i) for i in items)

    def pref_w(self) -> int:
        items = self.shown()
        pl, _, pr, _ = self._pads()
        if self.vertical:
            return min(self.max_w, pl + pr + max([0] + [best(i) for i in items]))
        return min(self.max_w, pl + pr + px(self.gap) * max(0, len(items) - 1) + sum(self._size(i) or best(i) for i in items))

    def widths(self, w: int) -> list[int]:
        """Each item's width in a row of width w: items that do not grow get what they ask for, those that grow share
        the rest; when it is short, growing items give way first, down to their least."""
        items = self.shown()
        pl, _, pr, _ = self._pads()
        avail = w - pl - pr - px(self.gap) * max(0, len(items) - 1)
        out = [0] * len(items)
        flex = []
        for k, i in enumerate(items):
            if isinstance(i, Spacer):
                out[k] = px(i.size)
                if i.grow:
                    flex.append(k)
            elif i.grow and i.fixed_w is None:
                flex.append(k)
            else:
                out[k] = best(i)
        rest = avail - sum(out)
        if flex:
            shares = share(rest, [(items[k].grow, 0 if isinstance(items[k], Spacer) else least(items[k]), items[k].max_w) for k in flex])
            for k, v in zip(flex, shares):
                out[k] += v
        over = sum(out) - avail
        if over > 0:  # still too wide: items that do not grow give way, those with the most room first
            room = [(k, out[k] - least(items[k])) for k in range(len(items)) if k not in flex and not isinstance(items[k], Spacer)
                    and out[k] > least(items[k])]
            total = sum(r for _, r in room)
            if total > 0:
                take = min(over, total)
                for k, r in room:
                    out[k] -= min(r, math.ceil(take * r / total))
        return out

    def height(self, w: int) -> int:
        items = self.shown()
        pl, pt, pr, pb = self._pads()
        if not items:
            return pt + pb
        if self.vertical:
            inner = w - pl - pr
            return pt + pb + px(self.gap) * (len(items) - 1) + sum(self._size(i) or i.height(self._col_w(i, inner)) for i in items)
        return pt + pb + max(i.height(iw) for i, iw in zip(items, self.widths(w)))

    def _col_w(self, i: Node, inner: int) -> int:
        if i.fixed_w is not None:
            return min(i.fixed_w, inner)
        if (i.align in ("", "fill") and i.fill_x) or i.align == "fill":
            return min(inner, i.max_w)
        return min(best(i), inner)

    # placing
    def place(self, x: int, y: int, w: int, h: int) -> None:
        items = self.shown()
        pl, pt, pr, pb = self._pads()
        if self.vertical:
            inner = w - pl - pr
            sizes = [self._size(i) or i.height(self._col_w(i, inner)) for i in items]
            extra = h - pt - pb - px(self.gap) * max(0, len(items) - 1) - sum(sizes)
            growing = [k for k, i in enumerate(items) if i.grow]
            if extra > 0 and growing:
                for k, v in zip(growing, share(extra, [(items[k].grow, 0, BIG) for k in growing])):
                    sizes[k] += v
            yy = y + pt
            for i, ih in zip(items, sizes):
                iw = self._col_w(i, inner)
                xx = x + pl + {"center": (inner - iw) // 2, "right": inner - iw}.get(i.align, 0)
                i.place(xx, yy, iw, ih)
                yy += ih + px(self.gap)
        else:
            inner = h - pt - pb
            xx = x + pl
            for i, iw in zip(items, self.widths(w)):
                ih = inner if i.align == "fill" else min(inner, i.height(iw))
                yy = y + pt + {"top": 0, "bottom": inner - ih}.get(i.align, (inner - ih) // 2)
                if i.align == "fill":
                    yy = y + pt
                i.place(xx, yy, iw, ih)
                xx += iw + px(self.gap)

    # windows
    def realize(self, host: wx.Window) -> None:
        for i in self.items:
            i.realize(host)

    def kill(self) -> None:
        for i in self.items:
            i.kill()

    def walk(self) -> Iterator[Node]:
        yield self
        for i in self.items:
            yield from i.walk()

    def sync_shown(self, visible: bool) -> None:
        for i in self.items:
            i.sync_shown(visible and not i.hidden)


def share(total: int, specs: list[tuple[int, int, int]]) -> list[int]:
    """total split by weight, each between its least and most (weight, least, most)."""
    n = len(specs)
    out: list[int | None] = [None] * n
    left = total
    free = list(range(n))
    while free:
        weight = sum(specs[k][0] for k in free) or 1
        fixed = False
        for k in list(free):
            g, lo, hi = specs[k]
            v = left * g / weight
            if v < lo or v > hi:
                out[k] = lo if v < lo else hi
                left -= out[k]  # type: ignore[operator]
                free.remove(k)
                fixed = True
        if not fixed:
            done = 0
            for n_, k in enumerate(free):
                v = int(left * specs[k][0] / weight) if n_ < len(free) - 1 else left - done
                out[k] = max(v, 0)
                done += v
            break
    return [v or 0 for v in out]


def vbox(pad: tuple = (0, 0, 0, 0), gap: int = 0) -> Box:
    return Box(True, pad, gap)


def hbox(pad: tuple = (0, 0, 0, 0), gap: int = 8) -> Box:
    return Box(False, pad, gap)


class Grid(Node):
    """Cells in rows and columns. A column that does not grow is as wide as its widest cell; growing columns share the
    rest equally by weight, whatever their text would ask for."""

    def __init__(self, cols: int, gap_x: int = 12, gap_y: int = 12):
        self.cols, self.gap_x, self.gap_y = cols, gap_x, gap_y
        self.cells: list[tuple[Node, int, int, int]] = []   # (node, row, col, span)
        self.col_grow = [0] * cols
        self.col_least = [0] * cols
        self.row_fill = False   # cells as tall as their row (tiles)

    def put(self, node: Node | None, row: int, col: int, span: int = 1, align: str = "") -> None:
        if node is None:
            return
        node.parent, node.align = self, align or node.align
        self.cells.append((node, row, col, span))

    def flow(self, nodes: list[Node]) -> None:
        for k, n in enumerate(nodes):
            self.put(n, k // self.cols, k % self.cols)

    def grow_col(self, col: int, n: int = 1) -> None:
        self.col_grow[col] = n

    def shown(self) -> list[tuple[Node, int, int, int]]:
        return [x for x in self.cells if not x[0].hidden]

    def _cols(self, w: int) -> list[int]:
        gx = px(self.gap_x)
        avail = w - gx * (self.cols - 1)
        out = [0] * self.cols
        lo = [px(v) for v in self.col_least]
        for node, _, col, span in self.shown():
            if span == 1:
                if not self.col_grow[col]:
                    out[col] = max(out[col], best(node))
                lo[col] = max(lo[col], least(node))
        for k in range(self.cols):
            if not self.col_grow[k]:
                out[k] = max(out[k], lo[k])
        flex = [k for k in range(self.cols) if self.col_grow[k]]
        rest = avail - sum(out)
        for k, v in zip(flex, share(rest, [(self.col_grow[k], lo[k], BIG) for k in flex])):
            out[k] = v
        over = sum(out) - avail
        if over > 0:
            room = [(k, out[k] - lo[k]) for k in range(self.cols) if not self.col_grow[k] and out[k] > lo[k]]
            total = sum(r for _, r in room)
            for k, r in room:
                out[k] -= min(r, math.ceil(min(over, total) * r / total))
        return out

    def _cell_w(self, cols: list[int], col: int, span: int) -> int:
        return sum(cols[col:col + span]) + px(self.gap_x) * (span - 1)

    def _rows(self, cols: list[int]) -> dict[int, int]:
        rows: dict[int, int] = {}
        for node, row, col, span in self.shown():
            cw = self._cell_w(cols, col, span)
            rows[row] = max(rows.get(row, 0), node.height(min(cw, best(node)) if node.align in ("left", "right", "center") else cw))
        return rows

    def min_w(self) -> int:
        lo = [px(v) for v in self.col_least]
        for node, _, col, span in self.shown():
            if span == 1:
                lo[col] = max(lo[col], least(node))
        return sum(lo) + px(self.gap_x) * (self.cols - 1)

    def pref_w(self) -> int:
        hi = [px(v) for v in self.col_least]
        for node, _, col, span in self.shown():
            if span == 1:
                hi[col] = max(hi[col], best(node))
        return sum(hi) + px(self.gap_x) * (self.cols - 1)

    def height(self, w: int) -> int:
        rows = self._rows(self._cols(w))
        return sum(rows.values()) + px(self.gap_y) * max(0, len(rows) - 1)

    def place(self, x: int, y: int, w: int, h: int) -> None:
        cols = self._cols(w)
        rows = self._rows(cols)
        tops, yy = {}, y
        for r in sorted(rows):
            tops[r] = yy
            yy += rows[r] + px(self.gap_y)
        lefts = [x + sum(cols[:k]) + px(self.gap_x) * k for k in range(self.cols)]
        for node, row, col, span in self.shown():
            cw = self._cell_w(cols, col, span)
            nw = min(cw, best(node)) if node.align in ("left", "right", "center") else cw
            nx = lefts[col] + {"right": cw - nw, "center": (cw - nw) // 2}.get(node.align, 0)
            nh = rows[row] if self.row_fill else node.height(nw)
            node.place(nx, tops[row], nw, nh)

    def realize(self, host: wx.Window) -> None:
        for node, *_ in self.cells:
            node.realize(host)

    def kill(self) -> None:
        for node, *_ in self.cells:
            node.kill()

    def walk(self) -> Iterator[Node]:
        yield self
        for node, *_ in self.cells:
            yield from node.walk()

    def sync_shown(self, visible: bool) -> None:
        for node, *_ in self.cells:
            node.sync_shown(visible and not node.hidden)


def grid(cols: int, gap: int = 12) -> Grid:
    return Grid(cols, gap, gap)


# ---------------------------------------------------------------- windows that paint themselves


@dataclass(frozen=True)
class Look:
    """A surface's paint: theme colour names for its fill, border and hover, its corner radius and which sides have
    the border."""
    fill: str | None = None
    border: str | None = None
    radius: int = 0
    sides: str = "all"           # all, top, bottom, right
    hover: str | None = None


LOOKS = {
    "plain": Look(),
    "page": Look("content"),
    "card": Look("card", "stroke", 8), "cardAlt": Look("card_alt", "stroke", 8),
    "subtle": Look("subtle", None, 6), "subtleSquare": Look("subtle"),
    "tile": Look("card", "stroke", 8, hover="control_hover"), "tileSmall": Look("subtle", None, 6),
    "groupHead": Look("card_alt", "divider", 0, "top"), "groupHeadTop": Look("card_alt"),
    "row": Look(None, "divider", 0, "top", "subtle"), "rowPlain": Look(None, "divider", 0, "top"), "rowBare": Look(),
    "rowFlat": Look(hover="subtle"),
    "needItem": Look("card", "stroke", 6, hover="subtle"),
    "banner_bad": Look("bad_bg", "bad_stroke", 8), "banner_warn": Look("warn_bg", "warn_stroke", 8),
    "banner_info": Look("card", "stroke", 8), "banner_ok": Look("ok_bg", "stroke", 8),
    "warnBox": Look("warn_bg", "warn_stroke", 6), "stopping": Look("neutral_bg", None, 4),
    "code": Look("code_bg"), "expanderBox": Look("card", "stroke", 6),
    "diffBefore": Look("subtle", None, 3), "diffAfter": Look("info_bg", None, 3),
    "segments": Look("subtle", "control_border", 4),
    "nav": Look("mica", "stroke", 0, "right"), "topbar": Look("mica", "stroke", 0, "bottom"),
    "flyout": Look("mica", "stroke"), "flyoutFoot": Look("mica", "stroke", 0, "top"), "trayLine": Look(None, None, 6, hover="hover"),
    "portal": Look("portal", "portal_stroke"), "portalFoot": Look("portal_foot", "portal_stroke", 0, "top"),
    "field": Look("control", "control_border", 4),
}

ROLE_TEXT, ROLE_BUTTON, ROLE_LINK, ROLE_PROGRESS, ROLE_GROUP, ROLE_LIST_ITEM, ROLE_MENU, ROLE_GRAPHIC = range(8)


class Win(Node):
    """A node with a window of its own."""

    look = LOOKS["plain"]
    focusable = False     # Tab stops here
    click_focus = True    # a click gives it the focus too (the navigation pane's items take it by Tab only)
    role = ROLE_GROUP
    fill_x = True
    min_h = 0             # its least height in design pixels: a list row's 48
    on_drag: Callable[[str, wx.Point], None] | None = None   # moves its window: ("down" | "move" | "up", pointer)

    def __init__(self) -> None:
        self.win: Canvas | None = None
        self.state = "new"            # new, live, dead
        self.content: Node | None = None
        self.on_click: Callable[[], None] | None = None
        self.enabled = True
        self.hover = False
        self.pressed = False
        self.tip = ""
        self.name = ""                # what a screen reader says
        self.description = ""
        self.copy_text = ""           # offered as Copy on a right click
        self.checked: bool | None = None

    # the box inside
    def inner(self) -> Box:
        assert isinstance(self.content, Box)
        return self.content

    def add(self, node: "Node | None", grow: int = 0, align: str = "") -> Any:
        return self.inner().add(node, grow, align)

    def space(self, n: int) -> None:
        self.inner().space(n)

    def spring(self, n: int = 1) -> None:
        self.inner().spring(n)

    # measuring
    def min_w(self) -> int:
        return self.content.min_w() if self.content else 0

    def pref_w(self) -> int:
        return min(self.max_w, self.content.pref_w()) if self.content else 0

    def height(self, w: int) -> int:
        return max(px(self.min_h), self.content.height(w) if self.content else 0)

    # windows
    def realize(self, host: wx.Window) -> None:
        if self.state != "new":
            return
        self.win = Canvas(host, self)
        self.state = "live"
        if self.content is not None:
            self.content.parent = self
            self.content.realize(self.win)
        if self.tip:
            self.win.SetToolTip(self.tip)
        if self.click_target() is not None:
            self.win.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        if self.own_hidden():
            self.win.Hide()

    def place(self, x: int, y: int, w: int, h: int) -> None:
        if self.win is None:
            return
        rect = wx.Rect(x, y, max(w, 0), max(h, 0))
        if self.win.GetRect() != rect:
            resized = self.win.GetSize() != rect.GetSize()
            self.win.SetSize(rect)
            if resized:  # what lies on it may have painted its corners or border
                self.refresh()
        if self.content is not None:
            self.content.place(0, 0, w, h)
        self.placed(w, h)

    def placed(self, w: int, h: int) -> None:
        """After a layout: what depends on the width (an ellipsis, a tooltip)."""

    def kill(self) -> None:
        win = self.win
        for n in self.walk():
            if isinstance(n, Win):
                n.state, n.win = "dead", None
        if win:
            win.node = None
            win.Hide()  # shown, it would keep a grey hole where each child was: Windows fills them as they go
            win.Destroy()

    def walk(self) -> Iterator[Node]:
        yield self
        if self.content is not None:
            yield from self.content.walk()

    def sync_shown(self, visible: bool) -> None:
        if self.win:
            self.win.Show(visible)
        elif self.content is not None:
            self.content.sync_shown(visible)

    def refresh(self) -> None:
        """Paints it again, and what lies on it."""
        if self.win:
            self.win.Refresh()
            for n in self.walk():
                if n is not self and isinstance(n, Win) and n.win:
                    n.win.Refresh()

    # input
    def click_target(self) -> "Win | None":
        n: Node | None = self
        while n is not None:
            if isinstance(n, Win) and n.on_click is not None:
                return n if n.enabled else None
            n = n.parent
        return None

    def drag_target(self) -> "Win | None":
        n: Node | None = self
        while n is not None:
            if isinstance(n, Win) and n.on_drag is not None:
                return n
            n = n.parent
        return None

    def activate(self) -> None:
        if self.on_click is not None and self.enabled and self.state == "live":
            self.on_click()

    def key_down(self, code: int) -> bool:
        """A key pressed while it has the focus; True when it used it. (Not key: that is the name it is found by.)"""
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE) and self.on_click is not None:
            wx.CallAfter(self.activate)
            return True
        return False

    def focus(self) -> None:
        if self.win and self.win.AcceptsFocus():
            self.win.SetFocus()

    def has_focus(self) -> bool:
        return bool(self.win) and wx.Window.FindFocus() is self.win

    # painting
    def fill(self) -> str | None:
        """The colour it paints over what lies behind, or None where that shows through."""
        if self.look.hover and self.hover and self.enabled and self.on_click is not None:
            return self.look.hover
        return self.look.fill

    def paint_back(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        look, fill = self.look, self.fill()
        r = px(look.radius)
        if fill:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(c(fill)))
            if r:
                gc.DrawRoundedRectangle(0, 0, w, h, r)
            else:
                gc.DrawRectangle(0, 0, w, h)
        if look.border:
            self.paint_border(gc, w, h)

    def paint_border(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        look = self.look
        t = max(1, px(1))
        gc.SetBrush(wx.Brush(c(look.border)))  # type: ignore[arg-type]
        gc.SetPen(wx.TRANSPARENT_PEN)
        if look.sides == "all":
            r = px(look.radius)
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(c(look.border), t))  # type: ignore[arg-type]
            if r:
                gc.DrawRoundedRectangle(t / 2, t / 2, w - t, h - t, r)
            else:
                gc.DrawRectangle(t / 2, t / 2, w - t, h - t)
        elif look.sides == "top":
            gc.DrawRectangle(0, 0, w, t)
        elif look.sides == "bottom":
            gc.DrawRectangle(0, h - t, w, t)
        elif look.sides == "right":
            gc.DrawRectangle(w - t, 0, t, h)

    def paint_edge(self, gc: wx.GraphicsContext, w: int, h: int, outside: wx.Colour) -> None:
        """Its rounded corners and border again, over a child that painted across them."""
        look = self.look
        r = px(look.radius)
        if r:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(outside))
            # each corner: from the corner to where its arc starts, along the arc, and back
            for (cx, cy), (ax, ay), a0 in (((0, 0), (r, r), math.pi), ((w, 0), (w - r, r), -math.pi / 2),
                                           ((w, h), (w - r, h - r), 0.0), ((0, h), (r, h - r), math.pi / 2)):
                p = gc.CreatePath()
                p.MoveToPoint(cx, cy)
                p.AddLineToPoint(ax + r * math.cos(a0), ay + r * math.sin(a0))
                p.AddArc(ax, ay, r, a0, a0 + math.pi / 2, True)
                p.CloseSubpath()
                gc.FillPath(p)
        if look.border:
            self.paint_border(gc, w, h)

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        """Its shapes and icons."""

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        """Its words."""

    def paint_focus(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        t = px(2)
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.SetPen(wx.Pen(c("focus"), t))
        gc.DrawRoundedRectangle(t / 2, t / 2, w - t, h - t, px(4))

    # screen readers
    def accessible_name(self) -> str:
        return self.name

    def accessible_value(self) -> str:
        return ""


# what the mouse is over, what it pressed and what it drags; whether focus came by keyboard
HOVER: Win | None = None
PRESSED: Win | None = None
DRAGGED: Win | None = None
KEYBOARD = False


def set_hover(n: Win | None) -> None:
    global HOVER
    if n is HOVER:
        return
    old, HOVER = HOVER, n
    for x in (old, n):
        if x is not None and x.state == "live":
            x.hover = x is n
            if x.look.hover or isinstance(x, Button):
                x.refresh()


def hover_of(win: wx.Window | None) -> Win | None:
    n = win.node if isinstance(win, Canvas) else None
    return n.click_target() if n is not None else None


def by_keyboard(on: bool) -> None:
    """Focus rings show after a key and hide after a click, as Windows does."""
    global KEYBOARD
    if KEYBOARD != on:
        KEYBOARD = on
        focused = wx.Window.FindFocus()
        if isinstance(focused, Canvas):
            focused.Refresh()


def outside_of(win: wx.Window) -> wx.Colour:
    """The colour behind a window: its nearest parent that paints a fill."""
    w = win.GetParent()
    while isinstance(w, Canvas):
        n = w.node
        if n is not None and n.fill():
            return c(n.fill())  # type: ignore[arg-type]
        w = w.GetParent()
    return w.GetBackgroundColour() if w else c("content")


class Canvas(wx.Control):
    """The window of a node: it paints what lies behind it, then the node."""

    def __init__(self, host: wx.Window, node: Win):
        super().__init__()  # made in two steps, its paint style first: else Windows erases it grey as it is born
        self.node: Win | None = node
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Create(host, style=wx.BORDER_NONE | wx.WANTS_CHARS | wx.FULL_REPAINT_ON_RESIZE)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        for ev in (wx.EVT_LEFT_DOWN, wx.EVT_LEFT_DCLICK):
            self.Bind(ev, self.on_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_RIGHT_UP, self.on_right)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: self.release())
        self.Bind(wx.EVT_ENTER_WINDOW, self.on_enter)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.on_leave)
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_wheel)
        self.Bind(wx.EVT_KEY_DOWN, self.on_key)
        self.Bind(wx.EVT_SET_FOCUS, self.on_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self.on_focus)
        if sys.platform == "win32" and ACCESSIBLE is not None:
            self.SetAccessible(ACCESSIBLE(self))

    def AcceptsFocus(self) -> bool:
        n = self.node
        return n is not None and n.focusable and n.enabled and self.IsShown()

    def AcceptsFocusFromKeyboard(self) -> bool:
        return self.AcceptsFocus()

    def DoGetBestClientSize(self) -> wx.Size:
        n = self.node
        if n is None:
            return wx.Size(1, 1)
        w = n.pref_w()
        return wx.Size(w, n.height(w))

    # painting
    def on_paint(self, e: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        n = self.node
        w, h = self.GetClientSize()
        if n is None or w <= 0 or h <= 0:
            return
        draw(self, n, dc, w, h)

    # mouse
    def on_down(self, e: wx.MouseEvent) -> None:
        global PRESSED, DRAGGED
        by_keyboard(False)
        n = self.node
        target = n.click_target() if n is not None else None
        if target is None:
            mover = n.drag_target() if n is not None else None
            if mover is None:
                e.Skip()
                return
            DRAGGED = mover
            if not self.HasCapture():
                self.CaptureMouse()
            mover.on_drag("down", wx.GetMousePosition())  # type: ignore[misc]
            return
        if target.focusable and target.click_focus:
            target.focus()
        PRESSED = target
        target.pressed = True
        target.refresh()
        if not self.HasCapture():
            self.CaptureMouse()

    def release(self) -> None:
        global PRESSED, DRAGGED
        if self.HasCapture():
            self.ReleaseMouse()
        if DRAGGED is not None:
            mover, DRAGGED = DRAGGED, None
            if mover.state == "live":
                mover.on_drag("up", wx.GetMousePosition())  # type: ignore[misc]
        if PRESSED is not None:
            PRESSED.pressed = False
            if PRESSED.state == "live":
                PRESSED.refresh()
        PRESSED = None

    def on_up(self, e: wx.MouseEvent) -> None:
        target = PRESSED
        self.release()
        if target is not None and target.win and target.state == "live":
            if target.win.GetScreenRect().Contains(wx.GetMousePosition()):
                wx.CallAfter(target.activate)
        else:
            e.Skip()

    def on_right(self, e: wx.MouseEvent) -> None:
        n = self.node
        if n is None or not n.copy_text:
            e.Skip()
            return
        menu = wx.Menu()
        item = menu.Append(wx.ID_COPY, "Copy")
        self.Bind(wx.EVT_MENU, lambda _e: copy_text(n.copy_text), item)
        self.PopupMenu(menu)
        menu.Destroy()

    def on_enter(self, e: wx.MouseEvent) -> None:
        set_hover(hover_of(self))
        e.Skip()

    def on_motion(self, e: wx.MouseEvent) -> None:
        if DRAGGED is not None and self.HasCapture():
            DRAGGED.on_drag("move", wx.GetMousePosition())  # type: ignore[misc]
            return
        self.on_enter(e)

    def on_leave(self, e: wx.MouseEvent) -> None:
        wx.CallAfter(lambda: set_hover(hover_of(wx.FindWindowAtPointer()[0])))
        e.Skip()

    def on_wheel(self, e: wx.MouseEvent) -> None:
        """The wheel scrolls the page it lies on, wherever the pointer is."""
        w = self.GetParent()
        while w is not None and not w.IsTopLevel():
            if hasattr(w, "wheel"):
                w.wheel(e)
                return
            w = w.GetParent()
        e.Skip()

    # keys and focus
    def on_key(self, e: wx.KeyEvent) -> None:
        by_keyboard(True)
        n = self.node
        if n is not None and n.key_down(e.GetKeyCode()):
            return
        e.Skip()

    def on_focus(self, e: wx.FocusEvent) -> None:
        self.Refresh()
        e.Skip()


def draw(win: wx.Window, n: Win, dc: wx.DC, w: int, h: int) -> None:
    """A node's window: what lies behind it, the node's fill, its parents' corners and borders that it covers, the
    node's shapes, its focus ring and its words."""
    gc = wx.GraphicsContext.Create(dc)
    chain = []
    x, y = win.GetPosition()
    p = win.GetParent()
    while isinstance(p, Canvas) and p.node is not None:
        chain.append((p, x, y))
        px_, py_ = p.GetPosition()
        x, y = x + px_, y + py_
        p = p.GetParent()
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.SetBrush(wx.Brush(p.GetBackgroundColour() if p else c("content")))
    gc.DrawRectangle(0, 0, w, h)
    for anc, ax, ay in reversed(chain):
        gc.PushState()
        gc.Translate(-ax, -ay)
        aw, ah = anc.GetClientSize()
        anc.node.paint_back(gc, aw, ah)
        gc.PopState()
    n.paint_back(gc, w, h)
    for anc, ax, ay in chain:  # a child over its parent's rounded corner or border keeps them
        look = anc.node.look
        if not (look.radius or look.border) or not n.fill():
            continue
        aw, ah = anc.GetClientSize()
        edge = max(px(look.radius), px(1))
        if ax >= edge and ay >= edge and ax + w <= aw - edge and ay + h <= ah - edge:
            continue
        gc.PushState()
        gc.Translate(-ax, -ay)
        anc.node.paint_edge(gc, aw, ah, outside_of(anc))
        gc.PopState()
    n.paint(gc, w, h)
    if KEYBOARD and n.focusable and wx.Window.FindFocus() is win:
        n.paint_focus(gc, w, h)
    gc.Flush()
    del gc
    n.paint_text(dc, w, h)


def set_tip(win: wx.Window, text: str) -> None:
    """A window's tooltip, or none."""
    if text:
        if win.GetToolTipText() != text:
            win.SetToolTip(text)
    elif win.GetToolTip() is not None:
        win.UnsetToolTip()


def copy_text(text: str) -> None:
    if wx.TheClipboard.Open():
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
        finally:
            wx.TheClipboard.Close()


# ---------------------------------------------------------------- screen readers (Windows)

ACCESSIBLE: Any = None
if sys.platform == "win32" and hasattr(wx, "Accessible"):
    ROLES = {ROLE_TEXT: "ROLE_SYSTEM_STATICTEXT", ROLE_BUTTON: "ROLE_SYSTEM_PUSHBUTTON", ROLE_LINK: "ROLE_SYSTEM_LINK",
             ROLE_PROGRESS: "ROLE_SYSTEM_PROGRESSBAR", ROLE_GROUP: "ROLE_SYSTEM_GROUPING", ROLE_LIST_ITEM: "ROLE_SYSTEM_LISTITEM",
             ROLE_MENU: "ROLE_SYSTEM_BUTTONMENU", ROLE_GRAPHIC: "ROLE_SYSTEM_GRAPHIC"}

    class _Accessible(wx.Accessible):  # type: ignore[misc]
        """Name, role, value and state of a node's window, and its click as the default action."""

        def __init__(self, win: Canvas):
            super().__init__(win)
            self.canvas = weakref.ref(win)

        def _node(self) -> Win | None:
            win = self.canvas()
            return win.node if win else None

        def GetName(self, child: int) -> Any:
            n = self._node()
            return (wx.ACC_OK, n.accessible_name()) if n is not None and child == 0 else (wx.ACC_NOT_IMPLEMENTED, "")

        def GetRole(self, child: int) -> Any:
            n = self._node()
            if n is None or child != 0:
                return (wx.ACC_NOT_IMPLEMENTED, 0)
            return (wx.ACC_OK, getattr(wx, ROLES.get(n.role, "ROLE_SYSTEM_GROUPING"), 0))

        def GetValue(self, child: int) -> Any:
            n = self._node()
            return (wx.ACC_OK, n.accessible_value()) if n is not None and child == 0 and n.accessible_value() else (wx.ACC_NOT_IMPLEMENTED, "")

        def GetDescription(self, child: int) -> Any:
            n = self._node()
            return (wx.ACC_OK, n.description) if n is not None and child == 0 and n.description else (wx.ACC_NOT_IMPLEMENTED, "")

        def GetState(self, child: int) -> Any:
            n, win = self._node(), self.canvas()
            if n is None or win is None or child != 0:
                return (wx.ACC_NOT_IMPLEMENTED, 0)
            state = 0
            if n.focusable:
                state |= wx.ACC_STATE_SYSTEM_FOCUSABLE
                if wx.Window.FindFocus() is win:
                    state |= wx.ACC_STATE_SYSTEM_FOCUSED
            if not n.enabled:
                state |= wx.ACC_STATE_SYSTEM_UNAVAILABLE
            if n.checked:
                state |= wx.ACC_STATE_SYSTEM_CHECKED
            if not win.IsShown():
                state |= wx.ACC_STATE_SYSTEM_INVISIBLE
            return (wx.ACC_OK, state)

        def GetDefaultAction(self, child: int) -> Any:
            n = self._node()
            return (wx.ACC_OK, "Press") if n is not None and n.on_click is not None else (wx.ACC_NOT_IMPLEMENTED, "")

        def DoDefaultAction(self, child: int) -> Any:
            n = self._node()
            if n is None or n.on_click is None:
                return wx.ACC_NOT_IMPLEMENTED
            wx.CallAfter(n.activate)
            return wx.ACC_OK

    ACCESSIBLE = _Accessible


# ---------------------------------------------------------------- text


class Text(Win):
    """Words in one role of the type ramp: on one line, wrapped to the width it gets, or cut with an ellipsis. Several
    runs make a line of mixed words, such as a bold name and muted words after it."""

    role = ROLE_TEXT

    def __init__(self, text: str = "", role: str = "body", tone: str = "", wrap: bool = False, elide: bool = False,
                 select: bool = False, runs: list[Run] | None = None, right: bool = False, underline: bool = False):
        super().__init__()
        self.runs = runs if runs is not None else [Run(text, role, tone)]
        self.wraps, self.elides, self.right = wrap, elide, right
        self.select = select
        self.underline = underline
        self._lines: tuple[int, list] | None = None
        self.shown_text = ""
        self.name = self.text()
        if select:
            self.copy_text = self.text()

    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def set_text(self, text: str) -> None:
        if len(self.runs) == 1 and self.runs[0].text == text:
            return
        before = (self.pref_w(), self.height(self.win.GetSize()[0]) if self.win else 0)
        self.runs = [Run(text, self.runs[0].role, self.runs[0].tone)]
        self._lines = None
        self.name = text
        if self.select:
            self.copy_text = text
        if before != (self.pref_w(), self.height(self.win.GetSize()[0]) if self.win else 0):
            self.request_layout()
        if self.win:
            self.placed(*self.win.GetSize())
            self.win.Refresh()

    def set_tone(self, tone: str) -> None:
        for r in self.runs:
            r.tone = tone
        if self.win:
            self.win.Refresh()

    def set_role(self, role: str) -> None:
        if self.runs[0].role != role:
            for r in self.runs:
                r.role = role
            self._lines = None
            self.request_layout()
            if self.win:
                self.win.Refresh()

    def lh(self) -> int:
        return max(line_h(r.role) for r in self.runs)

    def _plain_lines(self) -> list[str]:
        return self.text().split("\n")

    def pref_w(self) -> int:
        if len(self.runs) == 1:
            return max(text_w(line, self.runs[0].role) for line in self._plain_lines()) + 1
        return sum(text_w(r.text, r.role) for r in self.runs) + 1

    def min_w(self) -> int:
        if self.elides:
            return min(self.pref_w(), px(24))
        if self.wraps:
            words = [text_w(t.strip(), r.role) for r in self.runs for t in _tokens(r.text) if t.strip()]
            return min(self.pref_w(), max([px(40)] + words), px(160))
        return self.pref_w()

    def lines(self, w: int) -> list[list[tuple[int, str, Run]]]:
        if self._lines is None or self._lines[0] != w:
            self._lines = (w, wrap(self.runs, max(w, 1)))
        return self._lines[1]

    def height(self, w: int) -> int:
        if self.wraps:
            return len(self.lines(w)) * self.lh()
        return len(self._plain_lines()) * self.lh() if len(self.runs) == 1 else self.lh()

    def placed(self, w: int, h: int) -> None:
        cut = not self.wraps and self.pref_w() > w + 1
        if self.win:
            set_tip(self.win, self.text() if cut else self.tip)

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        lh = self.lh()
        if self.wraps:
            lines = self.lines(w)
        elif self.elides or self.pref_w() > w + 1:
            lines = [self._elided(w)]
        else:
            lines = [[(0, line, self.runs[0])] for line in self._plain_lines()] if len(self.runs) == 1 else \
                [[(x, r.text, r) for x, r in zip(self._xs(), self.runs)]]
        y = 0
        for line in lines:
            width = (line[-1][0] + text_w(line[-1][1], line[-1][2].role)) if line else 0
            shift = w - width - 1 if self.right else 0
            for x, t, r in line:
                f = font(r.role, SCALE)
                dc.SetFont(f.Underlined() if self.underline else f)
                dc.SetTextForeground(colour(tone_hex(r.tone)))
                dc.DrawText(t, x + shift, y + (lh - _height(r.role, SCALE)) // 2)
            y += lh

    def _xs(self) -> list[int]:
        xs, x = [], 0
        for r in self.runs:
            xs.append(x)
            x += text_w(r.text, r.role)
        return xs

    def _elided(self, w: int) -> list[tuple[int, str, Run]]:
        out, x = [], 0
        for r in self.runs:
            whole = text_w(r.text, r.role)
            if x + whole <= w:
                out.append((x, r.text, r))
                x += whole
                continue
            cut = elide(r.text, r.role, w - x)
            if cut:
                out.append((x, cut, r))
            break
        return out


def label(text: str = "", role: str = "body", tone: str = "", wrap: bool = False, select: bool = False, right: bool = False) -> Text:
    return Text(text, role, tone, wrap=wrap, select=select, right=right)


def elided(text: str, role: str = "body", tone: str = "") -> Text:
    """One line that ends in an ellipsis when it does not fit; the whole text on hover and for screen readers."""
    return Text(text, role, tone, elide=True)


def pair(strong: str, rest: str = "") -> Text:
    """A bold name and muted words after it, on one line that ends in an ellipsis ("Draft a change · bill · Claude")."""
    t = Text(runs=[Run(strong, "strong")] + ([Run(f" · {rest}", "body", "secondary")] if rest else []), elide=True)
    t.name = f"{strong}, {rest}" if rest else strong
    return t


def rich(runs: list[Run], wrap: bool = True) -> Text:
    return Text(runs=runs, wrap=wrap)


# ---------------------------------------------------------------- badges, avatars, bubbles, icons


class Badge(Win):
    """A status in a pill: icon and word, in a status colour. Screen readers read the word."""

    role = ROLE_TEXT
    fill_x = False

    def __init__(self, word: str, tone: str = "neutral", icon: str = "", height: int = 22):
        super().__init__()
        self.word, self.tone, self.icon = word, tone, icon
        self.h = height               # 24 at the head of the tray's panel
        self.name = word

    def set_word(self, word: str) -> None:
        if word != self.word:
            self.word = self.name = word
            self.request_layout()
            if self.win:
                self.win.Refresh()

    def pref_w(self) -> int:
        return text_w(self.word, "caption_strong") + px(16) + (px(16) if self.icon else 0)

    def min_w(self) -> int:
        return self.pref_w()

    def height(self, w: int) -> int:
        return px(self.h)

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        fg, bg = THEME.tone(self.tone)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour(bg)))
        gc.DrawRoundedRectangle(0, 0, w, h, px(4) if self.tone == "new" else h / 2)
        if self.icon:
            s = px(12)
            gc.DrawBitmap(icons.bitmap(self.icon, fg, s), px(8), (h - s) / 2, s, s)

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        fg, _ = THEME.tone(self.tone)
        dc.SetFont(font("caption_strong", SCALE))
        dc.SetTextForeground(colour(fg))
        dc.DrawText(self.word, px(8) + (px(16) if self.icon else 0), (h - _height("caption_strong", SCALE)) // 2)


class Fixed(Win):
    """A thing of a fixed size."""

    fill_x = False
    role = ROLE_GRAPHIC

    def __init__(self, w: int, h: int | None = None):
        super().__init__()
        self.size_ = (w, w if h is None else h)

    def pref_w(self) -> int:
        return px(self.size_[0])

    def min_w(self) -> int:
        return self.pref_w()

    def height(self, w: int) -> int:
        return px(self.size_[1])


class Avatar(Fixed):
    """An assistant's initials on its colour, the same everywhere (GL-3)."""

    def __init__(self, who: Any, size: int = 24):
        super().__init__(size)
        self.who = who
        self.name = who.name
        self.tip = who.name if not who.unknown else f"Unknown assistant, Windows user {who.user}"

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        _, bg = THEME.assistant(self.who.colour)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour(bg)))
        gc.DrawRoundedRectangle(0, 0, w, h, min(w / 4, px(8)))

    def font_role(self) -> str:
        if self.size_[0] >= 40:
            return "initials_large"
        if self.size_[0] > 28:
            return "initials_big"
        return "initials_small" if len(self.who.initials) > 1 else "initials"

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        fg, _ = THEME.assistant(self.who.colour)
        dc.SetFont(font(self.font_role(), SCALE))
        dc.SetTextForeground(colour(fg))
        tw, th = dc.GetTextExtent(self.who.initials)
        dc.DrawText(self.who.initials, (w - tw) // 2, (h - th) // 2)


class Bubble(Win):
    """A count beside a navigation item: accent for answers waiting, dark for problems, light beside a heading."""

    fill_x = False

    def __init__(self, n: int, tone: str = "accent"):
        super().__init__()
        self.n, self.tone = n, tone
        self.name = str(n)

    def pref_w(self) -> int:
        return max(px(20), text_w(str(self.n), "caption_strong") + px(12))

    def min_w(self) -> int:
        return self.pref_w()

    def height(self, w: int) -> int:
        return px(20)

    def colours(self) -> tuple[str, str]:
        k = THEME.c
        if self.tone == "accent":
            return k["accent"], k["on_accent"]
        if self.tone == "light":
            return k["neutral_bg"], k["text"]
        return (k["neutral"], "#FFFFFF") if not THEME.dark else ("#D6D6D6", "#000000")

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour(self.colours()[0])))
        gc.DrawRoundedRectangle(0, 0, w, h, h / 2)

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        dc.SetFont(font("caption_strong", SCALE))
        dc.SetTextForeground(colour(self.colours()[1]))
        tw, th = dc.GetTextExtent(str(self.n))
        dc.DrawText(str(self.n), (w - tw) // 2, (h - th) // 2)


class Icon(Fixed):
    """A decorative icon: hidden from screen readers (the text beside it says it)."""

    def __init__(self, name: str, tone: str = "", size: int = 16, colour_key: str = ""):
        super().__init__(size)
        self.icon, self.tone, self.colour_key = name, tone, colour_key

    def set_icon(self, name: str) -> None:
        self.icon = name
        if self.win:
            self.win.Refresh()

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        s = min(w, h)
        hexc = THEME.c[self.colour_key] if self.colour_key else tone_hex(self.tone)
        gc.DrawBitmap(icons.bitmap(self.icon, hexc, s), (w - s) / 2, (h - s) / 2, s, s)


class IconTile(Fixed):
    """An icon on a tinted square: the approval cards' and the tray lines' leading icon."""

    def __init__(self, name: str, tone: str = "warn", size: int = 32):
        super().__init__(size)
        self.icon, self.tone = name, tone

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        fg, bg = THEME.tone(self.tone)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour(bg)))
        gc.DrawRoundedRectangle(0, 0, w, h, px(6))
        s = px(16 if self.size_[0] <= 32 else 20)
        gc.DrawBitmap(icons.bitmap(self.icon, fg, s), (w - s) / 2, (h - s) / 2, s, s)


class Image(Fixed):
    """A picture, such as the kit's logo."""

    def __init__(self, image: wx.Image, size: int):
        super().__init__(size)
        self.image = image
        self._bmp: wx.Bitmap | None = None

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        if self._bmp is None or self._bmp.GetWidth() != w:
            self._bmp = wx.Bitmap(self.image.Scale(w, h, wx.IMAGE_QUALITY_HIGH))
        gc.DrawBitmap(self._bmp, 0, 0, w, h)


class Divider(Win):
    def height(self, w: int) -> int:
        return max(1, px(1))

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(c("divider")))
        gc.DrawRectangle(0, 0, w, h)


def divider() -> Divider:
    return Divider()


# ---------------------------------------------------------------- progress


class Bar(Win):
    """A progress bar: a real fraction, or None for "no progress known" (a moving bar; still with reduced motion)."""

    role = ROLE_PROGRESS

    def __init__(self, value: float | None = None, tone: str = "accent", height: int = 4, name: str = ""):
        super().__init__()
        self.value, self.tone, self.h = value, tone, height
        self.phase = 0.0
        self.name = name
        self.set(value)

    def pref_w(self) -> int:
        return px(40)

    def min_w(self) -> int:
        return px(16)

    def height(self, w: int) -> int:
        return px(self.h)

    def set(self, value: float | None) -> None:
        self.value = value
        if value is None and not REDUCED_MOTION:
            MOVING.add(self)
            ANIMATION.go()
        else:
            MOVING.discard(self)
        if self.win:
            self.win.Refresh()

    def accessible_value(self) -> str:
        return "progress unknown" if self.value is None else f"{round(self.value * 100)} percent"

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        k = THEME.c
        fill = {"accent": k["accent"], "warn": k["warn"], "neutral": k["secondary"], "ok": k["ok"]}.get(self.tone, k["accent"])
        rad = h / 2
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(c("track")))
        gc.DrawRoundedRectangle(0, 0, w, h, rad)
        gc.SetBrush(wx.Brush(colour(fill)))
        if self.value is None:
            if REDUCED_MOTION:
                return
            seg = w * 0.3
            x0 = (self.phase - 0.3) * w
            a, b = max(0.0, x0), min(float(w), x0 + seg)
            if b > a:
                gc.DrawRoundedRectangle(a, 0, b - a, h, rad)
        elif self.value > 0:
            gc.DrawRoundedRectangle(0, 0, max(float(h), w * min(1.0, self.value)), h, rad)


MOVING: "weakref.WeakSet[Bar]" = weakref.WeakSet()


class _Animation(wx.Timer):
    """One clock for every moving bar, running only while one moves."""

    def go(self) -> None:
        if not self.IsRunning() and wx.GetApp() is not None:
            self.Start(33)

    def Notify(self) -> None:
        live = [b for b in MOVING if b.state != "dead"]
        if not live:
            self.Stop()
            return
        for b in live:
            if b.win and b.win.IsShownOnScreen():
                b.phase = (b.phase + 0.012) % 1.4
                b.win.Refresh()


class _LazyAnimation:
    timer: _Animation | None = None

    def go(self) -> None:
        if self.timer is None:
            if wx.GetApp() is None:
                return
            self.timer = _Animation()
        self.timer.go()


ANIMATION = _LazyAnimation()


# ---------------------------------------------------------------- buttons


class Button(Win):
    """A button: standard, accent, quiet (the portal's), link, subtle (an icon alone), or a segment of a filter."""

    focusable = True
    role = ROLE_BUTTON
    fill_x = False

    def __init__(self, text: str = "", kind: str = "", icon: str = "", tooltip: str = "", on_click: Callable[[], None] | None = None,
                 size: int = 0, role_font: str = "", icon_right: bool = False, height: int = 0, pad: int = 0):
        super().__init__()
        self.text, self.kind, self.icon = text, kind, icon
        self.tip = tooltip
        self.on_click = on_click or (lambda: None)
        self.square = size            # an icon alone in a square of this size
        self.font_role = role_font or ("strong" if kind == "accent" else "body")
        self.icon_right = icon_right
        self.h = height
        self.pad = pad                # its padding left and right, when not its kind's
        self.name = text or tooltip
        if kind == "link":
            self.role = ROLE_LINK

    def set_enabled(self, on: bool) -> None:
        if self.enabled != on:
            self.enabled = on
            if self.win:
                self.win.SetCursor(wx.Cursor(wx.CURSOR_HAND if on else wx.CURSOR_DEFAULT))
                self.win.Refresh()

    def realize(self, host: wx.Window) -> None:
        super().realize(host)
        if self.win and not self.enabled:
            self.win.SetCursor(wx.Cursor(wx.CURSOR_DEFAULT))

    def _pad(self) -> int:
        return self.pad or {"link": 2, "subtle": 6, "seg": 14, "accent": 14 if self.h else 12}.get(self.kind, 12)

    def _icon_size(self) -> int:
        return px(16 if self.kind != "link" else 14)

    def pref_w(self) -> int:
        if self.square:
            return px(self.square)
        w = px(self._pad()) * 2
        if self.text:
            w += text_w(self.text, self.font_role)
        if self.icon:
            w += self._icon_size() + (px(8 if self.kind != "link" else 4) if self.text else 0)
        return w

    def min_w(self) -> int:
        return self.pref_w()

    def height(self, w: int) -> int:
        if self.square:
            return px(self.h or self.square)
        if self.h:
            return px(self.h)
        return {"link": line_h(self.font_role) + px(4), "subtle": px(32), "seg": px(32)}.get(self.kind, px(32))

    def colours(self) -> tuple[str | None, str | None, str]:
        """(fill, border, text) for its kind and state."""
        k = THEME.c
        if not self.enabled:
            if self.kind in ("link", "subtle"):
                return None, None, k["disabled"]
            return k["subtle"], k["disabled_border"], k["disabled"]
        if self.kind == "accent":
            fill = k["accent_pressed"] if self.pressed else k["accent_hover"] if self.hover else k["accent"]
            return fill, fill, k["on_accent"]
        if self.kind == "link":
            return None, None, k["accent"]
        if self.kind == "subtle":
            return (k["hover"] if self.hover or self.pressed else None), None, k["text"]
        if self.kind == "seg":
            return (k["card"] if self.checked else k["control_hover"] if self.hover else k["control"]), None, k["text"]
        base = k["quiet"] if self.kind == "quiet" else k["control"]
        fill = k["subtle"] if self.pressed else k["control_hover"] if self.hover else base
        return fill, k["control_border"], (k["secondary"] if self.pressed else k["text"])

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        fill, border, fg = self.colours()
        r = px(0 if self.kind == "seg" else 4)
        if fill:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(colour(fill)))
            gc.DrawRoundedRectangle(0, 0, w, h, r) if r else gc.DrawRectangle(0, 0, w, h)
        if border:
            t = max(1, px(1))
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(colour(border), t))
            gc.DrawRoundedRectangle(t / 2, t / 2, w - t, h - t, r)
        if self.icon:
            s = self._icon_size()
            if self.square or not self.text:
                x = (w - s) / 2
            elif self.icon_right:
                x = w - px(self._pad()) - s
            else:
                x = px(self._pad())
            gc.DrawBitmap(icons.bitmap(self.icon, fg, s), x, (h - s) / 2, s, s)

    def paint_focus(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        t = px(2) if self.kind != "link" else max(1, px(1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.SetPen(wx.Pen(c("focus"), t))
        gc.DrawRoundedRectangle(t / 2, t / 2, w - t, h - t, px(4))

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        if not self.text:
            return
        _, _, fg = self.colours()
        role = "strong" if self.kind == "seg" and self.checked else self.font_role
        f = font(role, SCALE)
        if self.kind == "link" and self.hover and self.enabled:
            f = f.Underlined()
        dc.SetFont(f)
        dc.SetTextForeground(colour(fg))
        x = px(self._pad())
        if self.icon and not self.icon_right:
            x += self._icon_size() + px(8 if self.kind != "link" else 4)
        tw, th = dc.GetTextExtent(self.text)
        dc.DrawText(self.text, x, (h - th) // 2)


def button(text: str, kind: str = "", icon: str = "", tooltip: str = "", on_click: Callable[[], None] | None = None) -> Button:
    return Button(text, kind, icon, tooltip, on_click)


def link(text: str, icon: str = "chevron_right", on_click: Callable[[], None] | None = None, role: str = "body") -> Button:
    return Button(text, "link", icon, on_click=on_click, role_font=role, icon_right=True)


def icon_button(icon: str, tooltip: str, on_click: Callable[[], None] | None = None, size: int = 32, name: str = "") -> Button:
    b = Button("", "subtle", icon, tooltip, on_click, size=size)
    b.name = name or tooltip
    return b


# ---------------------------------------------------------------- choosing from a list


class Choices(wx.PopupTransientWindow):
    """A drop-down list below a control: a name and a muted second line per choice, by mouse or by keyboard."""

    def __init__(self, anchor: wx.Window, items: list[tuple[Any, str, str]], current: Any, on_pick: Callable[[Any], None],
                 footer: list[tuple[Any, str]] | None = None, width: int = 0):
        super().__init__(anchor.GetTopLevelParent(), wx.BORDER_NONE)
        self.items = [(v, t, d, False) for v, t, d in items] + [(v, t, "", True) for v, t in (footer or [])]
        self.current, self.on_pick = current, on_pick
        self.at = next((k for k, it in enumerate(self.items) if it[0] == current), 0)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.rows = [px(48 if d else 36) for _, _, d, _ in self.items]
        self.gaps = [px(9) if foot and k and not self.items[k - 1][3] else 0 for k, (_, _, _, foot) in enumerate(self.items)]
        need = max([text_w(t, "strong") + px(48) for _, t, _, _ in self.items] + [text_w(d, "caption") + px(48) for _, _, d, _ in self.items])
        self.width = min(max(width, px(260), need), px(480))
        self.SetSize(self.width, sum(self.rows) + sum(self.gaps) + px(8))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_LEFT_UP, self.on_click)
        self.Bind(wx.EVT_KEY_DOWN, self.on_key)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)

    def row_at(self, y: int) -> int:
        top = px(4)
        for k, (h, g) in enumerate(zip(self.rows, self.gaps)):
            top += g
            if top <= y < top + h:
                return k
            top += h
        return -1

    def on_paint(self, e: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        gc = wx.GraphicsContext.Create(dc)
        gc.SetPen(wx.Pen(c("stroke"), max(1, px(1))))
        gc.SetBrush(wx.Brush(c("card")))
        gc.DrawRectangle(0, 0, w - 1, h - 1)
        top = px(4)
        boxes = []
        for k, (hh, g) in enumerate(zip(self.rows, self.gaps)):
            if g:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(c("divider")))
                gc.DrawRectangle(px(6), top + g // 2, w - px(12), max(1, px(1)))
            top += g
            if k == self.at:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(c("hover")))
                gc.DrawRoundedRectangle(px(4), top, w - px(8), hh, px(4))
            boxes.append((top, hh))
            top += hh
        gc.Flush()
        del gc
        for (v, t, d, foot), (y, hh) in zip(self.items, boxes):
            dc.SetFont(font("strong" if v == self.current and not foot else "body", SCALE))
            dc.SetTextForeground(c("text"))
            th = line_h("body")
            if d:
                dc.DrawText(elide(t, "strong", w - px(32)), px(14), y + px(6))
                dc.SetFont(font("caption", SCALE))
                dc.SetTextForeground(c("secondary"))
                dc.DrawText(elide(d, "caption", w - px(32)), px(14), y + px(6) + th)
            else:
                dc.DrawText(elide(t, "body", w - px(32)), px(14), y + (hh - _height("body", SCALE)) // 2)

    def on_motion(self, e: wx.MouseEvent) -> None:
        k = self.row_at(e.GetY())
        if k >= 0 and k != self.at:
            self.at = k
            self.Refresh()

    def on_click(self, e: wx.MouseEvent) -> None:
        k = self.row_at(e.GetY())
        if k >= 0:
            self.pick(k)

    def on_key(self, e: wx.KeyEvent) -> None:
        code = e.GetKeyCode()
        if code in (wx.WXK_DOWN, wx.WXK_UP):
            self.at = (self.at + (1 if code == wx.WXK_DOWN else -1)) % len(self.items)
            self.Refresh()
        elif code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self.pick(self.at)
        elif code == wx.WXK_ESCAPE:
            self.Dismiss()
        else:
            e.Skip()

    def pick(self, k: int) -> None:
        value = self.items[k][0]
        self.Dismiss()
        wx.CallAfter(self.on_pick, value)


class Dropdown(Button):
    """A button that opens a list of choices: Last 7 days, All assistants, the company file."""

    role = ROLE_MENU

    def __init__(self, choices: list[tuple[Any, str, str]], current: Any, icon: str = "", footer: list | None = None,
                 label_of: Callable[[Any], str] | None = None, on_pick: Callable[[Any], None] | None = None):
        self.choices, self.current, self.footer, self.label_of = choices, current, footer or [], label_of
        self.picked = on_pick or (lambda v: None)
        super().__init__(self._label(), "", icon, on_click=self.open, height=36)

    def _label(self) -> str:
        if self.label_of is not None:
            return self.label_of(self.current)
        return next((t for v, t, _ in self.choices if v == self.current), next((t for v, t in self.footer if v == self.current), ""))

    def pref_w(self) -> int:
        return text_w(self.text, "body") + px(10) * 2 + (px(24) if self.icon else 0) + px(20)

    def min_w(self) -> int:
        return min(self.pref_w(), px(150))

    def paint(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        fill, border, fg = self.colours()
        r = px(4)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour(fill or THEME.c["control"])))
        gc.DrawRoundedRectangle(0, 0, w, h, r)
        t = max(1, px(1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.SetPen(wx.Pen(colour(border or THEME.c["control_border"]), t))
        gc.DrawRoundedRectangle(t / 2, t / 2, w - t, h - t, r)
        s = px(16)
        if self.icon:
            gc.DrawBitmap(icons.bitmap(self.icon, fg, s), px(10), (h - s) / 2, s, s)
        cs = px(12)
        gc.DrawBitmap(icons.bitmap("chevron_down", fg, cs), w - px(10) - cs, (h - cs) / 2, cs, cs)

    def paint_text(self, dc: wx.DC, w: int, h: int) -> None:
        _, _, fg = self.colours()
        x = px(10) + (px(24) if self.icon else 0)
        dc.SetFont(font("body", SCALE))
        dc.SetTextForeground(colour(fg))
        shown = elide(self.text, "body", w - x - px(30))
        dc.DrawText(shown, x, (h - _height("body", SCALE)) // 2)

    def placed(self, w: int, h: int) -> None:
        if self.win:
            x = px(10) + (px(24) if self.icon else 0)
            cut = elide(self.text, "body", w - x - px(30)) != self.text
            set_tip(self.win, self.text if cut else self.tip)

    def open(self) -> None:
        if not self.win:
            return
        pop = Choices(self.win, self.choices, self.current, self._pick, self.footer, self.win.GetSize()[0])
        pop.Position(self.win.ClientToScreen((0, 0)), (0, self.win.GetSize()[1] + px(2)))
        pop.Popup()
        pop.SetFocus()

    def _pick(self, value: Any) -> None:
        self.current = value
        self.text = self.name = self._label()
        self.request_layout()
        if self.win:
            self.win.Refresh()
        self.picked(value)

    def key_down(self, code: int) -> bool:
        if code in (wx.WXK_DOWN, wx.WXK_F4):
            wx.CallAfter(self.open)
            return True
        return super().key_down(code)


# ---------------------------------------------------------------- surfaces: cards, rows, tiles, banners


class Surface(Win):
    """A box with a look of its own: a card, a coloured strip, a row."""

    def __init__(self, look: str = "plain", pad: tuple = (0, 0, 0, 0), gap: int = 0, horizontal: bool = False):
        super().__init__()
        self.look = LOOKS[look]
        self.content = Box(not horizontal, pad, gap)
        self.content.parent = self


def card(pad: tuple = (20, 18, 20, 20), gap: int = 12, look: str = "card") -> Surface:
    return Surface(look, pad, gap)


def box(look: str = "plain", pad: tuple = (0, 0, 0, 0), gap: int = 0, horizontal: bool = False) -> Surface:
    return Surface(look, pad, gap, horizontal)


class Row(Surface):
    """A line that opens something: the whole row is one link, by mouse, Enter or Space (the design's session rows)."""

    role = ROLE_LIST_ITEM

    def __init__(self, look: str = "row", pad: tuple = (20, 12, 20, 12), gap: int = 12, on_click: Callable[[], None] | None = None,
                 clickable: bool = True):
        super().__init__(look, pad, gap, horizontal=True)
        self.cells: list[Node] = []   # its fixed-width columns, which line_up gives one width down a list
        if clickable:
            self.focusable = True
            self.on_click = on_click or (lambda: None)


class Tile(Surface):
    """One of the Overview's four numbers; it opens its list."""

    def __init__(self, title: str, value: str, note: str = "", tone: str = "", icon: str = "", small: bool = False,
                 on_click: Callable[[], None] | None = None):
        super().__init__("tileSmall" if small else "tile", (14, 12, 14, 12) if small else (16, 14, 16, 16), 2)
        self.add(label(title, "caption", "secondary"))
        self.value = self.add(label(value, "big" if not small else "subtitle"))
        if note:
            row = hbox(gap=4)
            if icon:
                row.add(Icon(icon, tone, 12), align="top")
            row.add(label(note, "caption", tone or "secondary", wrap=True), 1)
            self.add(row)
        self.spring()
        self.name = f"{title}: {value}. {note}"
        if not small:
            self.focusable = True
            self.role = ROLE_LINK
            self.on_click = on_click or (lambda: None)


class Banner(Surface):
    """A coloured strip with an icon, a title in the status colour, words and an optional action (the States board)."""

    def __init__(self, tone: str, icon: str, title: str, text: str = "", more: str = "", action: Node | None = None):
        super().__init__(f"banner_{tone}", (16, 12, 16, 12), 10, horizontal=True)
        self.add(Icon(icon, tone if tone != "info" else "accent"), align="top")
        col = vbox(gap=2)
        col.add(rich([Run(title, "strong", tone if tone != "info" else "")] + ([Run(f" · {text}", "body")] if text else [])))
        if more:
            col.add(label(more, "body", "secondary", wrap=True))
        self.add(col, 1)
        if action is not None:
            self.add(action, align="top")
        self.name = f"{title}. {text} {more}".strip()


class Section(Surface):
    """A part of a screen that is built again only when what it shows changed, so focus, hover and open rows stay."""

    def __init__(self) -> None:
        super().__init__("plain")
        self.sig: Any = object()
        self.child: Node | None = None

    def show_built(self, sig: Any, build: Callable[[], "Node | None"]) -> bool:
        if sig == self.sig:
            return False
        self.sig = sig
        focused = wx.Window.FindFocus()
        key = ""
        if isinstance(focused, Canvas) and focused.node is not None and self._holds(focused.node):
            key = focused.node.key
        new = build()
        box_ = self.inner()
        if self.child is not None:
            self.child.kill()
            box_.items.clear()
        self.child = new
        self.hidden = new is None
        if new is not None:
            box_.add(new)
            if self.win:
                new.realize(self.win)
            if key:
                for n in new.walk():
                    if isinstance(n, Win) and n.key == key and n.focusable and n.win:
                        n.focus()
                        break
        if self.win:
            self.win.Show(not self.own_hidden())
        self.request_layout()
        return True

    def _holds(self, n: Node) -> bool:
        while n is not None:
            if n is self:
                return True
            n = n.parent  # type: ignore[assignment]
        return False


class Segments(Surface):
    """Filters side by side: All 7 | Saved 3 | Not saved 1."""

    fill_x = False

    def __init__(self, items: list[tuple[Any, str]], current: Any, on_pick: Callable[[Any], None]):
        super().__init__("segments", (1, 1, 1, 1), 0, horizontal=True)
        self.buttons: dict[Any, Button] = {}
        self.picked = on_pick
        for value, text in items:
            b = Button(text, "seg", on_click=lambda v=value: self._pick(v))
            b.checked = value == current
            b.key = f"seg:{value}"
            self.add(b)
            self.buttons[value] = b

    def _pick(self, value: Any) -> None:
        for v, b in self.buttons.items():
            b.checked = v == value
            if b.win:
                b.win.Refresh()
        self.picked(value)


class Field(Surface):
    """A box to type in, with a search icon and a clear button (Search by assistant or request)."""

    def __init__(self, placeholder: str, on_change: Callable[[str], None], name: str = "", most: int = 0):
        super().__init__("field", (10, 0, 4, 0), 8, horizontal=True)
        self.placeholder, self.changed = placeholder, on_change
        self.add(Icon("search", "secondary"))
        self.slot = self.add(Spacer(0), 1)
        self.clear = self.add(icon_button("x_plain", "Clear", self._clear, size=28))
        self.clear.hidden = True
        self.ctrl: wx.TextCtrl | None = None
        self.name = name or placeholder
        if most:
            self.max_w = px(most)

    def height(self, w: int) -> int:
        return px(36)

    def pref_w(self) -> int:
        return min(self.max_w, px(420))

    def min_w(self) -> int:
        return px(160)

    def realize(self, host: wx.Window) -> None:
        super().realize(host)
        if not self.win:
            return
        self.ctrl = wx.TextCtrl(self.win, style=wx.BORDER_NONE | wx.TE_PROCESS_ENTER)
        self.ctrl.SetHint(self.placeholder)
        self.ctrl.SetFont(font("body", SCALE))
        self.ctrl.SetName(self.name)
        self.retheme()
        self.ctrl.Bind(wx.EVT_TEXT, self._typed)
        self.ctrl.Bind(wx.EVT_SET_FOCUS, lambda e: (self.win.Refresh() if self.win else None, e.Skip()))
        self.ctrl.Bind(wx.EVT_KILL_FOCUS, lambda e: (self.win.Refresh() if self.win else None, e.Skip()))

    def retheme(self) -> None:
        if self.ctrl:
            self.ctrl.SetBackgroundColour(c("control"))
            self.ctrl.SetForegroundColour(c("text"))

    def value(self) -> str:
        return self.ctrl.GetValue() if self.ctrl else ""

    def _typed(self, e: wx.CommandEvent) -> None:
        self.clear.show(bool(self.value()))
        self.changed(self.value())

    def _clear(self) -> None:
        if self.ctrl:
            self.ctrl.SetValue("")
            self.ctrl.SetFocus()

    def place(self, x: int, y: int, w: int, h: int) -> None:
        super().place(x, y, w, h)
        if self.ctrl:
            ch = self.ctrl.GetBestSize()[1]
            left = px(10) + px(16) + px(8)
            right = px(4) + (px(28) + px(8) if not self.clear.hidden else 0)
            self.ctrl.SetSize(left, (h - ch) // 2, max(10, w - left - right), ch)

    def paint_back(self, gc: wx.GraphicsContext, w: int, h: int) -> None:
        super().paint_back(gc, w, h)
        focused = self.ctrl is not None and wx.Window.FindFocus() is self.ctrl
        t = px(2) if focused else max(1, px(1))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(c("accent" if focused else "secondary")))
        r = px(4)
        gc.PushState()
        gc.Clip(0, h - t, w, t)
        gc.DrawRoundedRectangle(0, h - r * 2, w, r * 2, r)
        gc.PopState()


# ---------------------------------------------------------------- times that follow the clock


class Ticker:
    """What follows the clock: labels whose text is "18 s" or "4 min ago", and other small updates. Each is tied to a
    node and dropped once that node's window is gone with its section."""

    def __init__(self) -> None:
        self.items: list[tuple[Node, Callable[[], Any]]] = []

    def add(self, n: Text, fn: Callable[[], str]) -> Text:
        """A text whose words are fn() each second."""
        n.set_text(fn())
        self.items.append((n, lambda: n.set_text(fn())))
        return n

    def every(self, owner: Node, fn: Callable[[], Any]) -> None:
        """fn() each second while owner lives."""
        fn()
        self.items.append((owner, fn))

    def tick(self) -> None:
        alive = []
        for n, fn in self.items:
            if isinstance(n, Win) and n.state != "live":
                continue
            fn()
            alive.append((n, fn))
        self.items = alive


# ---------------------------------------------------------------- roots: where a tree is laid out


class Root:
    """A window whose node tree is laid out as one: a page, the navigation pane, the tray panel, the portal."""

    def __init__(self, host: wx.Window, node: Win):
        self.host, self.node = host, node
        node.root = self  # type: ignore[attr-defined]
        self.pending = False
        node.realize(host)

    def schedule(self) -> None:
        if not self.pending:
            self.pending = True
            wx.CallAfter(self._run)

    def _run(self) -> None:
        if self.pending and self.host:
            self.pending = False
            self.layout()

    def layout(self) -> None:
        self.pending = False
        w, h = self.host.GetClientSize()
        if w > 0 and h > 0:
            self.host.Freeze()
            try:
                self.node.place(0, 0, w, h)
            finally:
                self.host.Thaw()


def focusables(top: wx.Window) -> list[wx.Window]:
    """The windows Tab visits, in order."""
    out: list[wx.Window] = []

    def walk(w: wx.Window) -> None:
        for ch in w.GetChildren():
            if ch.IsTopLevel() or not ch.IsShown():
                continue
            if isinstance(ch, Canvas):
                if ch.AcceptsFocusFromKeyboard():
                    out.append(ch)
            elif isinstance(ch, wx.TextCtrl) and ch.IsEnabled():
                out.append(ch)
            walk(ch)

    walk(top)
    return out


def tab(top: wx.Window, forward: bool) -> None:
    """Moves the focus to the next (or previous) control of a window."""
    stops = focusables(top)
    if not stops:
        return
    now = wx.Window.FindFocus()
    k = stops.index(now) if now in stops else (-1 if forward else 0)
    by_keyboard(True)
    nxt = stops[(k + (1 if forward else -1)) % len(stops)]
    nxt.SetFocus()
    nxt.Refresh()


def refresh_all(top: wx.Window) -> None:
    """Paints a window and everything in it again, after the theme changed."""
    top.Refresh()
    for ch in top.GetChildren():
        if not ch.IsTopLevel():
            refresh_all(ch)


def on_char_hook(top: wx.Window) -> Callable[[wx.KeyEvent], None]:
    """Tab and Shift+Tab for a top-level window."""
    def hook(e: wx.KeyEvent) -> None:
        if e.GetKeyCode() == wx.WXK_TAB and not e.ControlDown() and not e.AltDown():
            tab(top, not e.ShiftDown())
            return
        if e.GetKeyCode() not in (wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT):
            by_keyboard(True)
        e.Skip()
    return hook
