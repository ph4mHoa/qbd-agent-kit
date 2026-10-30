"""Figures for the person at the PC: a command's answer drawn as a page any web browser opens, so the person sees it at
a glance and the agent's reply can stay short. Written to the state folder next to the activity page. Plain HTML, CSS
and SVG with no scripts: the switches are CSS check boxes and the notes on hover are SVG titles. Nothing leaves the
PC."""
from __future__ import annotations

import datetime as dt
import html
import math
import os
import webbrowser
from pathlib import Path

from ..jsutil import money
from ..qb.dates import days_between
from .common import Ctx, company_facts

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SYMBOLS = {"US": "$", "CA": "$", "AU": "$", "UK": "£", "GB": "£"}
# fewest days late -> (label, class), oldest first: a customer's bar starts with its oldest money
AGES = [(91, "over 90 days late", "a4"), (61, "61–90", "a3"), (31, "31–60", "a2"), (1, "1–30", "a1"), (-10**9, "not due yet", "a0")]

# The cash figure's drawing, in SVG units: left margin for the ticks, the cash line's panel, the bars' panel, the dates.
W, L, R, TOP, HB, GAP, HR, XL = 760, 66, 26, 30, 140, 54, 190, 44

# No Cascadia Mono: Chrome 156 on Windows 10 1607 never finishes drawing a page set in it (the CascadiaMono.ttf that
# Visual Studio 2022 installs), with a renderer at 100% CPU. Consolas comes with every Windows.
STYLE = """
:root { --paper: #efece4; --card: #f8f6f0; --ink: #1d1b18; --ink-2: #55504a; --ink-3: #6b665c; --rule: #dcd7cb;
  --wash: #e6e1d6; --in: #2fa57a; --in-soft: #c6e7d8; --out: #e8684a; --flag: #f2b33d; --focus: #2d3b8e;
  --a0: #d5cfc2; --a1: #eb9a7c; --a2: #de7354; --a3: #bf4d35; --a4: #863022; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --paper: #191815; --card: #22201c; --ink: #ece7dc; --ink-2: #c9c2b5;
  --ink-3: #a59e91; --rule: #3a362f; --wash: #2d2a25; --in: #3dbb8c; --in-soft: #24493a; --out: #f07a5c;
  --flag: #f2b33d; --focus: #9aa8ff; --a0: #59544a; --a1: #7d3a2b; --a2: #a8492f; --a3: #d4603f; --a4: #f59a7a;
  color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 14px/1.5 Consolas, "SF Mono", Menlo, "DejaVu Sans Mono", monospace; }
main { max-width: 980px; margin: 0 auto; padding: 18px 16px 48px; display: flex; flex-direction: column; gap: 22px; }
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px 24px;
  padding-bottom: 12px; border-bottom: 1px solid var(--rule); }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; }
.brand svg { width: 24px; height: 24px; }
.brand path { stroke: var(--ink); stroke-width: 1.3; stroke-linejoin: round; }
.m-top { fill: var(--flag); } .m-l { fill: var(--in); } .m-r { fill: var(--out); } .m-slot { fill: none; stroke-linecap: round; }
.co { color: var(--ink-3); font-size: 12.5px; }
.fig { background: var(--card); border: 2px solid var(--ink); border-radius: 16px; box-shadow: 5px 5px 0 var(--ink);
  padding: 18px 20px 16px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.eyebrow { font-size: 10.5px; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-3); font-weight: 600; }
h1 { margin: 2px 0 4px; font-size: 21px; line-height: 1.25; }
.sub { margin: 0; color: var(--ink-2); font-size: 13px; max-width: 72ch; }
.tg { position: absolute; opacity: 0; width: 1px; height: 1px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.controls .gap { flex: 1 1 auto; }
.pill { font-size: 12.5px; font-weight: 600; color: var(--ink); background: var(--card); border: 1.5px solid var(--ink);
  border-radius: 999px; padding: 6px 12px; cursor: pointer; user-select: none; }
.pill:hover { background: var(--wash); }
#paid:checked ~ main label[for=paid], #see:checked ~ main label[for=see], #labels:checked ~ main label[for=labels] {
  background: var(--ink); color: var(--card); }
#paid:checked ~ main label[for=paid] { box-shadow: 3px 3px 0 var(--in); }
#paid:focus-visible ~ main label[for=paid], #see:focus-visible ~ main label[for=see],
#labels:focus-visible ~ main label[for=labels] { outline: 2px solid var(--focus); outline-offset: 2px; }
.v-now, .v-paid { display: flex; flex-direction: column; gap: 12px; }
.v-paid { display: none; }
#paid:checked ~ main .v-paid { display: flex; }
#paid:checked ~ main .v-now { display: none; }
.xray, .parts { display: none; }
#see:checked ~ main .xray { display: inline; }
#see:checked ~ main .parts { display: block; }
#see:checked ~ main .solid { display: none; }
#labels:not(:checked) ~ main .badges, #labels:not(:checked) ~ main .key { display: none; }
.chart svg { display: block; width: 100%; height: auto; }
.chart text { font-family: inherit; }
.grid { stroke: var(--rule); stroke-width: 1; }
.axis { stroke: var(--ink); stroke-width: 1.25; }
.zero { stroke: var(--out); stroke-width: 1.5; }
.cap { fill: var(--ink-3); font-size: 10.5px; letter-spacing: .09em; font-weight: 600; }
.tick, .xl2 { fill: var(--ink-3); font-size: 11px; }
.xl { fill: var(--ink-2); font-size: 11.5px; }
.val { fill: var(--ink-2); font-size: 11.5px; font-weight: 600; }
.lab { fill: var(--ink); font-size: 12px; font-weight: 700; }
.grid, .axis, .zero, .cap, .tick, .xl, .xl2, .val, .lab, .area, .badges, .case { pointer-events: none; }
.hit { fill: transparent; }
.area { fill: var(--ink); fill-opacity: .06; }
.bal { fill: none; stroke: var(--ink); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { fill: var(--ink); stroke: var(--card); stroke-width: 2; }
.dot.low { fill: var(--flag); stroke: var(--ink); stroke-width: 1.5; }
.bar { stroke: var(--ink); stroke-width: 1.25; stroke-linejoin: round; }
.in { fill: var(--in); } .out { fill: var(--out); }
.extra { fill: var(--in-soft); stroke: var(--ink); stroke-width: 1.25; stroke-dasharray: 4 3; }
.case { fill: none; stroke: var(--ink); stroke-width: 1.25; }
.piece { stroke: var(--ink); stroke-width: 1; fill-opacity: .55; }
.badges circle { fill: var(--card); stroke: var(--ink); stroke-width: 1.5; }
.badges text { fill: var(--ink); font-size: 11px; font-weight: 700; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--ink-2); align-items: center; }
.sw { display: inline-block; width: 12px; height: 12px; border: 1.25px solid var(--ink); border-radius: 3px;
  vertical-align: -1px; margin-right: 6px; }
.sw.in { background: var(--in); } .sw.out { background: var(--out); }
.sw.extra { background: var(--in-soft); border-style: dashed; }
.sw.line { width: 16px; height: 2px; border: 0; border-radius: 1px; background: var(--ink); vertical-align: 3px; }
.sw.a0 { background: var(--a0); } .sw.a1 { background: var(--a1); } .sw.a2 { background: var(--a2); }
.sw.a3 { background: var(--a3); } .sw.a4 { background: var(--a4); }
.key { list-style: none; margin: 0; padding: 10px 0 0; border-top: 1px solid var(--rule); display: flex;
  flex-direction: column; gap: 6px; font-size: 13px; }
.key li { display: flex; gap: 10px; align-items: baseline; }
.num { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px;
  border: 1.5px solid var(--ink); border-radius: 50%; font-size: 11px; font-weight: 700; }
.parts { border-top: 1px solid var(--rule); padding-top: 6px; }
.week { display: flex; flex-wrap: wrap; gap: 4px 28px; padding: 8px 0; border-bottom: 1px dashed var(--rule); }
.week h2 { flex: 1 1 100%; margin: 0; font-size: 13px; }
.week h2 small, .muted { color: var(--ink-3); font-weight: 400; }
.side { flex: 1 1 260px; min-width: 0; }
.side .eyebrow { margin-bottom: 2px; }
.doc { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: 12.5px; }
.doc small { display: block; color: var(--ink-3); font-size: 11.5px; }
.amt { white-space: nowrap; font-variant-numeric: tabular-nums; }
.late { margin-left: 6px; font-size: 10.5px; border: 1px solid var(--ink-3); border-radius: 999px; padding: 0 6px; }
.none { margin: 3px 0; color: var(--ink-3); font-size: 12.5px; }
details summary { cursor: pointer; }
.more summary { font-size: 12.5px; color: var(--ink-2); }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 8px; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--rule); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); font-weight: 600; }
.notes { margin: 8px 0 0; padding-left: 18px; color: var(--ink-2); font-size: 12.5px; }
.source { margin: 0; font-size: 11.5px; color: var(--ink-3); }
code { font-family: inherit; background: var(--wash); padding: 0 4px; border-radius: 4px; }
footer { color: var(--ink-3); font-size: 12px; }
.who { border-bottom: 1px solid var(--rule); }
.who summary { list-style: none; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; padding: 10px 0; }
.who summary::-webkit-details-marker { display: none; }
.who summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.w-name { flex: 1 1 15rem; min-width: 0; }
.w-name b::before { content: "+"; display: inline-block; width: 1.3em; color: var(--ink-3); }
.who[open] .w-name b::before { content: "−"; }
.w-name small { display: block; padding-left: 1.3em; color: var(--ink-3); font-size: 12px; }
.w-bar { flex: 3 1 14rem; min-width: 8rem; }
.track { display: flex; gap: 2px; height: 14px; }
.seg { min-width: 3px; border: 1.25px solid var(--ink); border-radius: 3px; }
.seg.a0 { background: var(--a0); } .seg.a1 { background: var(--a1); } .seg.a2 { background: var(--a2); }
.seg.a3 { background: var(--a3); } .seg.a4 { background: var(--a4); }
.w-amt { flex: 0 0 9rem; text-align: right; }
.w-amt b { display: block; font-variant-numeric: tabular-nums; }
.w-amt small { color: var(--ink-3); font-size: 12px; }
.w-docs { padding: 0 0 12px 1.3em; }
.w-docs p { margin: 6px 0 0; color: var(--ink-2); font-size: 12.5px; }
"""

MARK = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path class="m-top" d="M12 3 21 8 12 13 3 8Z"/>'
        '<path class="m-l" d="M3 8 12 13V22L3 17Z"/><path class="m-r" d="M12 13 21 8V17L12 22Z"/>'
        '<path class="m-slot" d="M9.6 7.3 14.4 9.9"/></svg>')


def esc(x: object) -> str:
    return html.escape(str(x))


def _m(v: float, sym: str) -> str:
    """-1234.5 -> '−$1,234.50'."""
    return ("−" if v < 0 else "") + sym + money(abs(v))


def _whole(v: float, sym: str) -> str:
    return ("−" if v < 0 else "") + sym + f"{abs(v):,.0f}"


def _short(v: float, sym: str) -> str:
    """Axis ticks: 175000 -> '$175k', 2500 -> '$2.5k', 1500000 -> '$1.5M'."""
    a = abs(v)
    s = f"{a / 1e6:,.1f}M" if a >= 1e6 else f"{a / 1e3:,.1f}k" if a >= 1e3 else f"{a:,.0f}"
    return ("−" if v < 0 else "") + sym + s.replace(".0M", "M").replace(".0k", "k")


def _day(iso: str) -> str:
    _, m, d = (int(x) for x in iso.split("-"))
    return f"{MONTHS[m - 1]} {d}"


def _span(a: str, b: str) -> str:
    return f"{_day(a)}–{int(b[8:])}" if a[:7] == b[:7] else f"{_day(a)} – {_day(b)}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _nice(raw: float) -> float:
    """The round step (1, 2, 2.5 or 5 times a power of ten) at or above raw."""
    if raw <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(raw))
    return next(m * p for m in (1, 2, 2.5, 5, 10) if m * p >= raw * (1 - 1e-9))


def _domain(lo: float, hi: float) -> tuple[float, float, float]:
    """A cash scale with round ticks and some room above and below the line."""
    step = _nice((hi - lo) / 3 if hi > lo else max(abs(hi) * .05, 100.0))
    a, b = math.floor(lo / step) * step, math.ceil(hi / step) * step
    if lo - a < step * .2:
        a -= step
    if b - hi < step * .2:
        b += step
    return a, b, step


def _who(ctx: Ctx) -> tuple[str, str]:
    """The currency sign for the QuickBooks country, and the company's name."""
    try:
        sym = SYMBOLS.get(ctx.qb.host_info().country, "")
        name = company_facts(ctx).name
    except Exception:  # noqa: BLE001 - the page then goes without them
        return "", ""
    return sym, name


def show(page: str) -> bool:
    """Opens the page in the web browser on the desktop people see. agy on Windows runs each command on a desktop of
    its own, where a browser started from here would open unseen, so from there the browser starts on the Default
    desktop."""
    uri = Path(page).resolve().as_uri()
    if os.name == "nt":
        try:
            import win32api
            import win32con
            import win32process
            import win32service

            here = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
            if win32service.GetUserObjectInformation(here, win32con.UOI_NAME).lower() != "default":
                si = win32process.STARTUPINFO()
                si.lpDesktop = "WinSta0\\Default"
                win32process.CreateProcess(None, f"rundll32.exe url.dll,FileProtocolHandler {uri}", None, None, False, 0, None, None, si)
                return True
        except Exception:  # noqa: BLE001 - then as on any other PC
            pass
    return webbrowser.open(uri)


def publish(ctx: Ctx, name: str, title: str, company: str, as_of: str, body: str) -> dict:
    """Writes the page to the state folder and opens it in the web browser."""
    cfg = ctx.config
    page = os.path.join(cfg.state_dir, f"{name}-demo.html" if cfg.demo else f"{name}.html")
    made = dt.datetime.now()
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body>{body[0]}<main>
<header class="top"><div class="brand">{MARK}<span>qbd</span></div>
<div class="co">{esc(company) + " · " if company else ""}{_day(as_of)}, {as_of[:4]}</div></header>
{body[1]}
<footer>Drawn by qbd from QuickBooks on this PC at {made:%H:%M}; nothing on this page leaves it. To see it again with
newer numbers, run <code>qbd {esc(name)} --open</code>.</footer>
</main></body></html>
"""
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(doc)
    return {"page": page, "opened": show(page)}


# ---------------------------------------------------------------- qbd cash-position --open


def _bar(x: float, y0: float, y1: float, w: float, cls: str, tip: str, top: bool = False, bottom: bool = False) -> str:
    """A bar from y0 down to y1, its data end rounded and its baseline end square."""
    r = min(4.0, (y1 - y0) / 2, w / 2)
    a, b = (r if top else 0), (r if bottom else 0)
    d = (f"M{x:.1f} {y0 + a:.1f}Q{x:.1f} {y0:.1f} {x + a:.1f} {y0:.1f}H{x + w - a:.1f}Q{x + w:.1f} {y0:.1f} {x + w:.1f} {y0 + a:.1f}"
         f"V{y1 - b:.1f}Q{x + w:.1f} {y1:.1f} {x + w - b:.1f} {y1:.1f}H{x + b:.1f}Q{x:.1f} {y1:.1f} {x:.1f} {y1 - b:.1f}Z")
    return f'<path class="{cls}" d="{d}">' + (f"<title>{esc(tip)}</title>" if tip else "") + "</path>"


def _doc_tip(d: dict, sym: str, as_of: str) -> str:
    what = " ".join(x for x in (d["type"].lower(), d["ref"] if isinstance(d["ref"], str) else "") if x)
    return f"{d['party']} · {what} · due {_day(d['due'])}{' · late' if d['due'] < as_of else ''}: {_m(d['left'], sym)}"


def _pieces(docs: list[dict], k: float) -> list[list[dict]]:
    """The documents inside a bar, largest first; those too thin to see go together at the end."""
    big = sorted(docs, key=lambda d: -d["left"])
    keep = [[d] for d in big if d["left"] * k >= 5]
    rest = [d for d in big if d["left"] * k < 5]
    return keep + ([rest] if rest else [])


def _cash_svg(s: dict, dom: tuple[float, float, float], sym: str, as_of: str) -> tuple[str, list[str]]:
    """One state of the cash figure: the SVG, and the notes its numbered marks point to."""
    table, start, extra = s["table"], s["start"], s["extra"]
    n = len(table)
    col = (W - L - R) / n
    bw = min(28.0, col * .36)
    lo, hi, step = dom
    yb = lambda v: TOP + HB - (v - lo) / (hi - lo) * HB
    ins = [w["money_in"] - (extra if i == 0 else 0) for i, w in enumerate(table)]
    outs = [w["money_out"] for w in table]
    up_raw, dn_raw = max([ins[0] + extra, *ins[1:]]), max(outs)
    big = max(up_raw, dn_raw, 1.0)
    up, dn = max(up_raw, big * .2), max(dn_raw, big * .2)
    rtop, pad = TOP + HB + GAP, 22  # pad: room for the values above and below the bars
    k = (HR - 2 * pad) / (up + dn)
    base = rtop + pad + up * k
    cx = lambda i: L + (i + .5) * col
    ends = [start] + [w["closing_estimate"] for w in table]
    pts = [(L + i * col, yb(v)) for i, v in enumerate(ends)]
    o = [f'<svg viewBox="0 0 {W} {TOP + HB + GAP + HR + XL}" role="img" aria-label="{esc(s["summary"])}">']

    for i, w in enumerate(table):  # under everything: a whole week answers on hover
        tip = (f"{_span(w['week_start'], w['week_end'])}: money in {_m(w['money_in'], sym)}, money out {_m(w['money_out'], sym)}, "
               f"cash at week end {_m(w['closing_estimate'], sym)}")
        o.append(f'<rect class="hit" x="{L + i * col:.1f}" y="{TOP - 4}" width="{col:.1f}" height="{rtop + HR + XL - TOP:.1f}"><title>{esc(tip)}</title></rect>')

    o.append(f'<text class="cap" x="{L}" y="{TOP - 14}">CASH AT WEEK END</text>')
    for j in range(round((hi - lo) / step) + 1):
        v = lo + j * step
        y = yb(v)
        o.append(f'<line class="{"zero" if lo < 0 < hi and abs(v) < step / 1e6 else "grid"}" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        o.append(f'<text class="tick" x="{L - 14}" y="{y + 4:.1f}" text-anchor="end">{_short(v, sym)}</text>')
    line = " ".join(f"{'L' if i else 'M'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    o.append(f'<path class="area" d="{line} L{pts[-1][0]:.1f} {TOP + HB} L{pts[0][0]:.1f} {TOP + HB} Z"/><path class="bal" d="{line}"/>')
    low = s["lowest"]
    low_i = 0 if low["week_end"] == as_of else 1 + next(i for i, w in enumerate(table) if w["week_end"] == low["week_end"])
    for i, (x, y) in enumerate(pts):
        tip = f"Cash now: {_m(start, sym)}" if i == 0 else f"Cash at the end of {_span(table[i - 1]['week_start'], table[i - 1]['week_end'])}: {_m(ends[i], sym)}"
        o.append(f'<circle class="dot{" low" if i == low_i else ""}" cx="{x:.1f}" cy="{y:.1f}" r="{6 if i == low_i else 4}"><title>{esc(tip)}</title></circle>')
    x0, y0 = pts[0]
    o.append(f'<text class="lab" x="{x0 + 12:.1f}" y="{y0 + 26 if pts[1][1] < y0 else y0 - 16:.1f}">{_whole(start, sym)} now</text>')
    xn, yn = pts[-1]
    o.append(f'<text class="lab" x="{xn - 4:.1f}" y="{yn + 26 if pts[-2][1] < yn else yn - 16:.1f}" text-anchor="end">{_whole(ends[-1], sym)}</text>')

    o.append(f'<text class="cap" x="{L}" y="{rtop - 16}">MONEY IN AND OUT, BY DUE DATE</text>')
    gstep = _nice(big / 2)
    for sign, room in ((-1, base - rtop), (1, rtop + HR - base)):
        v = gstep
        while v * k <= room:
            y = base + sign * v * k
            o.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="tick" x="{L - 14}" y="{y + 4:.1f}" text-anchor="end">{"+" if sign < 0 else "−"}{_short(v, sym)}</text>')
            v += gstep
    o.append(f'<line class="axis" x1="{L}" x2="{W - R}" y1="{base:.1f}" y2="{base:.1f}"/>'
             f'<text class="tick" x="{L - 14}" y="{base + 4:.1f}" text-anchor="end">{sym}0</text>')

    solid, xray, both, labels = [], [], [], []
    late_in = s["late_in"]
    for i, w in enumerate(table):
        x, when = cx(i) - bw / 2, _span(w["week_start"], w["week_end"])
        hin, hx, hout = ins[i] * k, (extra if i == 0 else 0) * k, outs[i] * k
        docs_in, docs_out = s["docs"][i]["in"], s["docs"][i]["out"]
        if hin >= .5:
            solid.append(_bar(x, base - hin, base, bw, "bar in", f"Money in, {when}: {_m(ins[i], sym)} from {_plural(len(docs_in), 'invoice')}", top=hx < .5))
            xray.append(_bar(x, base - hin, base, bw, "case", "", top=hx < .5))
            y = base
            for g in _pieces(docs_in, k):
                h = sum(d["left"] for d in g) * k
                tip = _doc_tip(g[0], sym, as_of) if len(g) == 1 else f"{_plural(len(g), 'smaller invoice')}: {_m(sum(d['left'] for d in g), sym)}"
                gap = 1 if h > 3 else 0
                xray.append(f'<rect class="piece in" x="{x + 3:.1f}" y="{y - h + gap:.1f}" width="{bw - 6:.1f}" height="{max(h - 2 * gap, .8):.1f}"><title>{esc(tip)}</title></rect>')
                y -= h
        if hx >= .5:
            labels_n = len({d["party"] for d in late_in})
            both.append(_bar(x, base - hin - hx, base - hin, bw, "bar extra",
                              f"Late invoices, if paid this week: {_m(extra, sym)} from {_plural(len(late_in), 'invoice')} to {_plural(labels_n, 'customer')}", top=True))
        if hout >= .5:
            solid.append(_bar(x, base, base + hout, bw, "bar out", f"Money out, {when}: {_m(outs[i], sym)} for {_plural(len(docs_out), 'bill')}", bottom=True))
            xray.append(_bar(x, base, base + hout, bw, "case", "", bottom=True))
            y = base
            for g in _pieces(docs_out, k):
                h = sum(d["left"] for d in g) * k
                tip = _doc_tip(g[0], sym, as_of) if len(g) == 1 else f"{_plural(len(g), 'smaller bill')}: {_m(sum(d['left'] for d in g), sym)}"
                gap = 1 if h > 3 else 0
                xray.append(f'<rect class="piece out" x="{x + 3:.1f}" y="{y + gap:.1f}" width="{bw - 6:.1f}" height="{max(h - 2 * gap, .8):.1f}"><title>{esc(tip)}</title></rect>')
                y += h
        fmt = _whole if n <= 6 else _short
        if ins[i] + (extra if i == 0 else 0) > 0:
            labels.append(f'<text class="val" x="{cx(i):.1f}" y="{base - hin - hx - 6:.1f}" text-anchor="middle">{fmt(ins[i] + (extra if i == 0 else 0), sym)}</text>')
        if outs[i] > 0:
            labels.append(f'<text class="val" x="{cx(i):.1f}" y="{base + hout + 15:.1f}" text-anchor="middle">{fmt(outs[i], sym)}</text>')
        if not w["money_in"] and not w["money_out"]:
            labels.append(f'<text class="tick" x="{cx(i):.1f}" y="{base - 6:.1f}" text-anchor="middle">{"nothing due" if col >= 90 else "–"}</text>')
        o.append(f'<text class="xl" x="{cx(i):.1f}" y="{rtop + HR + 18}" text-anchor="middle">{when if n <= 6 else _day(w["week_start"])}</text>'
                 f'<text class="xl2" x="{cx(i):.1f}" y="{rtop + HR + 33}" text-anchor="middle">{f"week {i + 1}" if n <= 6 else i + 1}</text>')
    o.append(f'<g class="solid">{"".join(solid)}</g><g class="xray">{"".join(xray)}</g>{"".join(both + labels)}')

    notes, marks = [], []
    if low["amount"] < 0:
        notes.append(f"Cash runs out: <b>{_m(low['amount'], sym)}</b> in the week ending {_day(low['week_end'])}.")
    elif low_i == 0:
        notes.append(f"Lowest point: today, <b>{_m(start, sym)}</b>; every week ends higher.")
    else:
        notes.append(f"Lowest point: <b>{_m(low['amount'], sym)}</b>, in the week ending {_day(low['week_end'])}.")
    marks.append(pts[low_i])
    late_out = [d for d in s["docs"][0]["out"] if d["due"] < as_of]
    edge = cx(0) + bw / 2 + 14
    if late_out:
        notes.append(f"{_plural(len(late_out), 'late bill')} ({_m(sum(d['left'] for d in late_out), sym)}): paid in the first week.")
        marks.append((edge, base + max(outs[0] * k / 2, 9)))
    if extra:
        notes.append(f"{_plural(len(late_in), 'late invoice')} from {_plural(len({d['party'] for d in late_in}), 'customer')} "
                     f"({_m(extra, sym)}): counted as paid in the first week.")
        marks.append((edge, base - ins[0] * k - extra * k / 2))
    o.append('<g class="badges">' + "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9"/><text x="{x:.1f}" y="{y + 3.8:.1f}" text-anchor="middle">{j + 1}</text>'
        for j, (x, y) in enumerate(marks)) + "</g></svg>")
    return "".join(o), notes


def _doc_row(d: dict, sym: str, as_of: str) -> str:
    what = " ".join(x for x in (d["type"].lower(), d["ref"] if isinstance(d["ref"], str) else "") if x)
    late = '<span class="late">late</span>' if d["due"] < as_of else ""
    return (f'<div class="doc"><span>{esc(d["party"])}<small>{esc(what)} · due {_day(d["due"])}{late}</small></span>'
            f'<span class="amt">{_m(d["left"], sym)}</span></div>')


def _parts(s: dict, sym: str, as_of: str) -> str:
    """What is inside each week's bars, as a list: the see-through view's parts list."""
    out = []
    for i, w in enumerate(s["table"]):
        docs_in = sorted(s["docs"][i]["in"], key=lambda d: -d["left"])
        rows_in = [_doc_row(d, sym, as_of) for d in docs_in]
        if i == 0 and s["extra"]:
            by: dict[str, list[dict]] = {}
            for d in s["late_in"]:
                by.setdefault(d["party"], []).append(d)
            for party, ds in sorted(by.items(), key=lambda kv: -sum(d["left"] for d in kv[1])):
                rows_in.append(f'<div class="doc"><span>{esc(party)}<small>{_plural(len(ds), "late invoice")}, if paid now</small></span>'
                               f'<span class="amt">{_m(sum(d["left"] for d in ds), sym)}</span></div>')
        rows_out = [_doc_row(d, sym, as_of) for d in sorted(s["docs"][i]["out"], key=lambda d: -d["left"])]
        none = '<p class="none">Nothing due.</p>'
        out.append(
            f'<div class="week"><h2>Week {i + 1} · {_span(w["week_start"], w["week_end"])} '
            f'<small>cash at week end {_m(w["closing_estimate"], sym)}</small></h2>'
            f'<div class="side"><div class="eyebrow">Coming in · {_m(w["money_in"], sym)}</div>{"".join(rows_in) or none}</div>'
            f'<div class="side"><div class="eyebrow">Going out · {_m(w["money_out"], sym)}</div>{"".join(rows_out) or none}</div></div>')
    return f'<div class="parts">{"".join(out)}</div>'


def _numbers(s: dict, sym: str, as_of: str) -> str:
    rows = [f"<tr><td>Today, {_day(as_of)}</td><td></td><td></td><td>{_m(s['start'], sym)}</td></tr>"] + [
        f"<tr><td>{_span(w['week_start'], w['week_end'])}</td><td>{_m(w['money_in'], sym)}</td><td>{_m(w['money_out'], sym)}</td>"
        f"<td>{_m(w['closing_estimate'], sym)}</td></tr>" for w in s["table"]]
    return ('<details class="more"><summary>Show the numbers</summary><div class="wrap"><table><thead><tr><th>Week</th>'
            f'<th>Money in</th><th>Money out</th><th>Cash at week end</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def cash(ctx: Ctx, result: dict, now: tuple, paid: tuple, late_in: list[dict]) -> dict:
    """The page for qbd cash-position --open. now and paid are the forecast with late invoices left out and counted
    in the first week: each its weeks, its lowest point and the invoices and bills behind each week."""
    sym, company = _who(ctx)
    as_of, start = result["as_of"], result["starting_cash"]["total"]
    late = round(sum(d["left"] for d in late_in), 2)
    states = {"v-now": now} if not late_in else {"v-now": now, "v-paid": paid}
    ends = [w["closing_estimate"] for table, _, _ in states.values() for w in table]
    dom = _domain(min([start, *ends]), max([start, *ends]))  # one scale for both, so the switch moves the line
    n = len(now[0])
    blocks = []
    for cls, (table, lowest, docs) in states.items():
        s = {"table": table, "lowest": lowest, "docs": docs, "start": start, "late_in": late_in,
             "extra": late if cls == "v-paid" else 0}
        s["summary"] = (f"Cash from {_m(start, sym)} now to {_m(table[-1]['closing_estimate'], sym)} on {_day(table[-1]['week_end'])}; "
                        f"lowest {_m(lowest['amount'], sym)}. Each week's money in and out is in the table below.")
        svg, notes = _cash_svg(s, dom, sym, as_of)
        legend = ('<div class="legend"><span><span class="sw in"></span>Money in: invoices due</span>'
                  '<span><span class="sw out"></span>Money out: bills due</span><span><span class="sw line"></span>Cash at week end</span>'
                  + ('<span><span class="sw extra"></span>Late invoices, if paid</span>' if s["extra"] else "") + "</div>")
        key = "".join(f'<li><span class="num">{j + 1}</span><span>{t}</span></li>' for j, t in enumerate(notes))
        blocks.append(f'<div class="{cls}"><div class="chart">{svg}</div>{legend}<ol class="key">{key}</ol>'
                      f'{_parts(s, sym, as_of)}{_numbers(s, sym, as_of)}</div>')
    switches = (f'<input class="tg" type="checkbox" id="paid"{" checked" if result["overdue_receivables"]["counted"] else ""}>' if late_in else "") + \
        '<input class="tg" type="checkbox" id="see"><input class="tg" type="checkbox" id="labels" checked>'
    controls = ('<div class="controls">' + (f'<label class="pill" for="paid">Count late invoices as paid · +{_m(late, sym)}</label>' if late_in else "")
                + '<span class="gap"></span><label class="pill" for="see">See-through</label><label class="pill" for="labels">Labels</label></div>')
    assumptions = "".join(f"<li>{esc(x)}</li>" for x in result["assumptions"])
    body = f"""<section class="fig" aria-labelledby="t">
<div><div class="eyebrow">Cash forecast</div><h1 id="t">Cash for the next {_plural(n, "week")}</h1>
<p class="sub">Cash now, plus customer invoices on their due dates, minus bills on theirs. Payroll, card spending and
loan payments are not in it.</p></div>
{controls}{"".join(blocks)}
<details class="more"><summary>How this is estimated</summary><ul class="notes">{assumptions}</ul></details>
<p class="source">From QuickBooks: <code>qbd cash-position</code>, as of {_day(as_of)}, {as_of[:4]}. Cash now is
{_m(result["starting_cash"]["bank"], sym)} in the bank and {_m(result["starting_cash"]["undeposited_funds"], sym)} received but not deposited.</p>
</section>"""
    return publish(ctx, "cash-position", f"Cash for the next {_plural(n, 'week')}", company, as_of, (switches, body))


# ---------------------------------------------------------------- qbd receivables --open


def _customer(c: dict, widest: float, sym: str, as_of: str) -> str:
    docs = [d for d in c["docs"] if d["left"] > 0]
    ages: dict[str, list] = {}
    for d in docs:
        late = days_between(d["due"], as_of)
        _, label, cls = next(a for a in AGES if late >= a[0])
        ages.setdefault(cls, [label, 0.0, 0])
        ages[cls][1] += d["left"]
        ages[cls][2] += 1
    owed = sum(d["left"] for d in docs)
    segs = "".join(f'<span class="seg {cls}" style="flex-grow:{ages[cls][1]:.2f}" title="{esc(ages[cls][0])}: '
                   f'{_m(ages[cls][1], sym)}, {_plural(ages[cls][2], "invoice")}"></span>' for _, _, cls in AGES if cls in ages)
    if c["overdue"] > 0:
        amount, about = _m(c["overdue"], sym), f"{c['oldest']} days late"
    else:
        amount, about = _m(c["balance"], sym), "not due yet"
    step = esc(c["next_step"]) + (" · apply the credit first" if c["credit"] else "")
    rows = "".join(
        f"<tr><td>{esc(' '.join(x for x in ('' if 'invoice' in d['type'].lower() else d['type'], d['ref'] if isinstance(d['ref'], str) else '') if x))}</td>"
        f"<td>{_day(d['due'])}, {d['due'][:4]}</td><td>{max(0, days_between(d['due'], as_of)) or 'not due'}</td><td>{_m(d['left'], sym)}</td></tr>"
        for d in docs)
    credit = (f"<p>{_m(c['credit'], sym)} in payments or credits is not applied to these invoices yet: apply it in QuickBooks "
              "(Customers &gt; Receive Payments) before sending a reminder.</p>") if c["credit"] else ""
    return (f'<details class="who"><summary><span class="w-name"><b>{esc(c["customer"])}</b><small>{step}</small></span>'
            f'<span class="w-bar"><span class="track" style="width:{100 * owed / widest:.1f}%">{segs}</span></span>'
            f'<span class="w-amt"><b>{amount}</b><small>{about}</small></span></summary>'
            f'<div class="w-docs"><div class="wrap"><table><thead><tr><th>Invoice</th><th>Due</th><th>Days late</th><th>Left to pay</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>{credit}</div></details>')


def receivables(ctx: Ctx, result: dict, customers: list[dict]) -> dict:
    """The page for qbd receivables --open. customers: the ones the answer lists, each with all its open invoices."""
    sym, company = _who(ctx)
    as_of, t = result["as_of"], result["totals"]
    widest = max([sum(d["left"] for d in c["docs"] if d["left"] > 0) for c in customers] + [0.01])
    legend = "".join(f'<span><span class="sw {cls}"></span>{esc(label)}</span>' for _, label, cls in AGES)
    late = (f"{t['customers_overdue']} of the {t['customers_owing']} customers who owe are late, with {_m(t['overdue'], sym)} overdue."
            if t["customers_overdue"] else "Nobody is late.")
    # What the customers who owe owe: QuickBooks' total takes off credit beyond some customers' invoices, so it can be
    # less than what is overdue.
    owed = round(t["open"] + t.get("credit_balances", 0), 2)
    notes = []
    if "credit_balances" in t:
        notes.append(f"QuickBooks' total is {_m(t['open'], sym)}: it takes off {_m(t['credit_balances'], sym)} that customers "
                     f"paid beyond what they owe.")
    if "more_customers" in result:
        notes.append(f"{_plural(result['more_customers'], 'more customer')} owe less; <code>qbd receivables --top N --open</code> lists more.")
    if result.get("customers_in_credit"):
        notes.append(f"{_plural(len(result['customers_in_credit']), 'customer')} paid or got credits that cover their invoices: nothing to chase.")
    body = f"""<section class="fig" aria-labelledby="t">
<div><div class="eyebrow">Collections</div><h1 id="t">Who owes you</h1>
<p class="sub">Customers owe {_m(owed, sym)}. {late} Open a row to see its invoices.</p></div>
<div class="legend">{legend}</div>
<div>{"".join(_customer(c, widest, sym, as_of) for c in customers) or '<p class="none">No customer owes money.</p>'}</div>
{f'<ul class="notes">{"".join(f"<li>{x}</li>" for x in notes)}</ul>' if notes else ""}
<p class="source">From QuickBooks: <code>qbd receivables</code>, as of {_day(as_of)}, {as_of[:4]}. The bar is what each customer
owes, oldest money first; the next step follows how late the oldest invoice is.</p>
</section>"""
    return publish(ctx, "receivables", "Who owes you", company, as_of, ("", body))
