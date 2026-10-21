#!/usr/bin/env python3
"""Render the README's pain-point GIFs from real `qbd --demo` answers (the built-in sample company).

Each GIF shows a question, types the real command, shows its real answer (cut down to the fields that matter, with
"…" where something was left out) and ends with one plain-words line built from the answer's own numbers.

Needs Pillow (pip install pillow) and the DejaVu, Consolas/Segoe UI or Menlo/Helvetica fonts. Writes assets/<scene>.gif.

usage: python3 scripts/make-demo-gifs.py [scene ...]
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, PAD, LINE = 960, 24, 21
BG, BAR, BORDER, TEXT, DIM = "#0d1117", "#161b22", "#30363d", "#c9d1d9", "#8b949e"
KEY, STR, NUM, PROMPT, CMD = "#79c0ff", "#a5d6ff", "#ffa657", "#7ee787", "#f0f6fc"
ASK_BG, ASK, SAYS_BG, SAYS = "#172a45", "#e6edf3", "#12261e", "#aff5b4"
FONTS = {
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf", "/System/Library/Fonts/Menlo.ttc"],
    "mono_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "C:/Windows/Fonts/consolab.ttf", "/System/Library/Fonts/Menlo.ttc"],
    "sans": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/Helvetica.ttc"],
    "sans_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf", "/System/Library/Fonts/Helvetica.ttc"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = next((p for p in FONTS[kind] if os.path.exists(p)), None)
    if path is None:
        sys.exit(f"No {kind} font found; tried {', '.join(FONTS[kind])}")
    return ImageFont.truetype(path, size)


MONO, MONO_BOLD = font("mono", 15), font("mono_bold", 15)
SANS, SANS_BOLD, SMALL = font("sans", 16), font("sans_bold", 18), font("sans", 13)
CHAR = MONO.getlength("M")
COLS = int((W - 2 * PAD) // CHAR)


def money(n: float) -> str:
    return f"{n:,.2f}"


# ---- The answer, cut down and laid out like `qbd --pretty` prints it.

def cut(v: Any, spec: Any) -> Any:
    """Keep what spec names: True keeps a value whole, {key: spec} keeps those keys, [spec, n] keeps the first n items,
    an int keeps the first n characters of a text. What is left out is marked with "…"."""
    if spec is True:
        return v
    if isinstance(spec, int) and isinstance(v, str):
        return v if len(v) <= spec else v[:v.rfind(" ", 0, spec)] + " …"
    if isinstance(spec, dict):
        out = {k: cut(x, spec[k]) for k, x in v.items() if k in spec}
        return {**out, "…": None} if len(out) < len(v) else out
    if isinstance(spec, list):
        sub, n = spec
        return [cut(x, sub) for x in v[:n]] + ([f"… {len(v) - n} more"] if len(v) > n else [])
    raise ValueError(spec)


def flat(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join("…" if k == "…" else f"{json.dumps(k)}: {flat(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(flat(x) for x in v) + "]"
    if isinstance(v, str) and v.startswith("… ") and v.endswith(" more"):
        return v
    return json.dumps(v, ensure_ascii=False)


def layout(v: Any, indent: int = 0, head: str = "", tail: str = "") -> list[str]:
    """Pretty JSON lines. A value that fits on its line stays on one line, and so does an object of plain values that
    fits on two; text with line breaks is shown on several lines (marked CONT for colouring)."""
    pad = "  " * indent
    if isinstance(v, str) and "\n" in v:
        first, *rest = v.split("\n")
        hang = " " * (len(pad) + len(head) + 1)
        return [f'{pad}{head}"{first}', *(f"{CONT}{hang}{r}" for r in rest[:-1]), f'{CONT}{hang}{rest[-1]}"{tail}']
    one = f"{pad}{head}{flat(v)}{tail}"
    plain = isinstance(v, dict) and not any(isinstance(x, (dict, list)) for x in v.values())
    if len(one) <= COLS or (plain and len(one) <= 2 * COLS - len(pad) - 4) or not isinstance(v, (dict, list)) or not v:
        return [one]
    lines = [f"{pad}{head}{'{' if isinstance(v, dict) else '['}"]
    items = list(v.items()) if isinstance(v, dict) else [(None, x) for x in v]
    for i, (k, x) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if k == "…" or (isinstance(x, str) and x.startswith("… ") and x.endswith(" more")):
            lines.append(f"{pad}  {'…' if k == '…' else x}")
        else:
            lines += layout(x, indent + 1, f"{json.dumps(k)}: " if k is not None else "", comma)
    return lines + [f"{pad}{'}' if isinstance(v, dict) else ']'}{tail}"]


CONT = "\x01"
TOKEN = re.compile(r'(?P<key>"(?:[^"\\]|\\.)*"(?=: ))|(?P<str>"(?:[^"\\]|\\.)*"|"[^"]*\Z)|(?P<num>-?\d+(?:\.\d+)?\b|true|false|null)'
                   r'|(?P<dim>… \d+ more|…|[{}\[\],:])')


def colored(line: str) -> list[tuple[str, str]]:
    if line.lstrip().startswith("#"):
        return [(line, DIM)]
    if line.startswith(CONT):
        return [(line[1:], STR)]
    out, at = [], 0
    for m in TOKEN.finditer(line):
        if m.start() > at:
            out.append((line[at:m.start()], TEXT))
        out.append((m.group(0), {"key": KEY, "str": STR, "num": NUM, "dim": DIM}[m.lastgroup]))
        at = m.end()
    return out + ([(line[at:], TEXT)] if at < len(line) else [])


def wrapped(segments: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Soft-wraps a colored line at COLS, at a space where there is one, with the continuation indented."""
    chars = [(c, color) for text, color in segments for c in text]
    indent = len(chars) - len("".join(c for c, _ in chars).lstrip()) + 2
    rows, first = [], True
    while chars:
        room = COLS if first else COLS - indent
        if len(chars) <= room:
            take = len(chars)
        else:
            space = max((i for i in range(room) if chars[i][0] == " "), default=0)
            take = space + 1 if space > room // 2 else room
        row = ([] if first else [(" " * indent, TEXT)]) + [(c, color) for c, color in chars[:take]]
        rows.append(row)
        chars, first = chars[take:], False
    return rows or [[]]


# ---- Scenes: each runs real commands and says what the answer shows.

@dataclass
class Step:
    cmd: list[str]
    out: list[str]
    says: str
    ask: str | None = None
    asker: str = "You ask your agent"
    note: str | None = None


def deposits(run: Callable) -> list[Step]:
    cmd = ["deposits", "--amount", "3231.95", "--account", "Business Checking"]
    a = run(cmd)
    m = a["match"]
    paid = " + ".join(f"{p['customer']} {money(p['amount'])}" for p in m["payments"])
    return [Step(cmd, layout(cut(a, {"waiting": True, "deposit": True, "next_step": True, "match": {
        "how": True, "confidence": True, "total": True, "payments": [{"date": True, "method": True, "customer": True, "ref": True, "amount": True}, 6]}})),
        f"{paid} = {money(m['total'])} ({m['confidence']} confidence), out of {a['waiting']['payments']} payments waiting "
        f"to be deposited. The deposit is ready to draft.",
        ask="The bank shows a deposit of 3,231.95. Which payments are in it?")]


def match_payment(run: Callable) -> list[Step]:
    first = ["match-payment", "Granite Peak Contractors", "1749.19", "--memo", "Inv 1046, 1094"]
    a = run(first)
    m = a["match"]
    with open(os.path.join(run.cwd, "payment.json"), "w", encoding="utf-8") as f:
        json.dump(a["draft_fields"], f)
    second = ["draft", "add", "receive_payment", "--fields", "@payment.json", "--reason", "Check from Granite Peak"]
    d = run(second, writes=True)
    third = ["commit", d["token"]]
    c = run(third, writes=True)
    applied = " and ".join(f"{i['ref']} ({money(i['apply'])})" for i in m["invoices"])
    return [
        Step(first, layout(cut(a, {"customer": True, "amount": True, "open": True, "match": {
            "how": True, "confidence": True, "invoices": [{"ref": True, "due": True, "open": True, "apply": True}, 6]}, "draft_fields": True})),
            f"It pays invoices {applied}: {m['how']}, {m['confidence']} confidence.",
            ask="Granite Peak Contractors paid 1,749.19 by check, memo \"Inv 1046, 1094\". Record it."),
        Step(second, layout(cut(d, {"token": True, "expires_in_minutes": True, "preview": True, "next_step": True})),
             "Checked against the books and nothing saved yet: the agent shows you this preview and waits for your yes.",
             note="# the agent saved draft_fields from the answer above as payment.json"),
        Step(third, layout(cut(c, {"done": True, "summary": True, "ids": {"TxnID": True}, "undo": True})),
             "Saved after your yes and written to the audit log; qbd undo with the same token drafts its reversal.",
             ask="Looks right. Save it.", asker="You reply"),
    ]


def stock_history(run: Callable) -> list[Step]:
    cmd = ["stock-history", "LED Flood Light 50W"]
    a = run(cmd)
    times = []
    for b in a["below_zero"]:
        s = b["started_by"]
        t = f"{s['type'].lower()} {s['ref']} ({-s['qty']:g} units on {s['date']}) took it to {s['on_hand_after']:g}"
        if b.get("still_below_zero"):
            t += " and it is still below zero"
        else:
            t += f"; it stayed below zero {b['days']} days, down to {b['lowest']:g}, until {b['ended_by']['type'].lower()} {b['ended_by']['ref']}"
        times.append(t)
    who = {"date": True, "type": True, "ref": True, "name": True, "qty": True, "on_hand_after": True}
    return [Step(cmd, layout(cut(a, {"on_hand": True, "below_zero": [{
        "started_by": who, "lowest": True, "days": True, "ended_by": who, "still_below_zero": True}, 4], "next_step": 150})),
        ("Twice: " if len(times) == 2 else "") + "; ".join(times) + ". The fix for each comes with it.",
        ask="Why is the LED flood light at -3?")]


def health_check(run: Callable) -> list[Step]:
    cmd = ["health-check", "--period-end", "2026-09-30"]
    a = run(cmd)
    warn = [c["title"] for c in a["checks"] if c["status"] == "warn"]
    return [Step(cmd, layout(cut(a, {"summary": True, "checks": [{"status": True, "finding": True}, 12]})),
                 f"{a['summary']['warnings']} warnings before closing, each with its fix: " + " · ".join(warn) + ".",
                 ask="Check the books before I close September.")]


def uncleared(run: Callable) -> list[Step]:
    cmd = ["uncleared", "--account", "Business Checking"]
    a = run(cmd)
    kinds = {"Check": "Check", "Bill Pmt -Check": "Bill payment"}
    said = [f"{kinds.get(u['type'], u['type'])} {u['ref']} to {u['name']} ({money(-u['amount'])}) has not cleared in {u['days']} days."
            for u in a["uncleared"]]
    said += [f"{kinds.get(p['type'], p['type'])} {p['ref']} to {p['name']} ({money(-p['amount'])}) is stuck in a reconciliation "
             "that was never finished." for p in a.get("pending", [])]
    row = {"date": True, "type": True, "ref": True, "name": True, "amount": True, "days": True}
    return [Step(cmd, layout(cut(a, {"accounts": True, "uncleared": [row, 6], "pending": [row, 6], "pending_note": True})),
                 " ".join(said), ask="Which checks still haven't cleared the bank?")]


def daily_brief(run: Callable) -> list[Step]:
    cmd = ["daily-brief"]
    a = run(cmd)
    cash, owed, sales = a["cash"], a["receivables"], a["sales"]
    return [Step(cmd, layout(cut(a, {
        "cash": {"bank_total": True, "undeposited_funds": True},
        "receivables": {"total_owed": True, "overdue": True, "over_90": True, "customers_overdue": True},
        "payables": {"open_total": True, "overdue_total": True, "due_next_7_days": True},
        "sales": {"income_change_pct": True},
        "inventory": {"below_reorder_point": True, "negative_on_hand": True},
        "attention": [True, 6]})),
        f"{money(cash['bank_total'])} in the bank and {money(cash['undeposited_funds'])} waiting to deposit; "
        f"{money(owed['overdue'])} overdue from {owed['customers_overdue']} customers; sales {abs(sales['income_change_pct']):g}% "
        f"{'below' if sales['income_change_pct'] < 0 else 'above'} the same days last month. Then what needs attention today.",
        ask="Give me today's finance brief.")]


SCENES = {"daily-brief": daily_brief, "deposits": deposits, "match-payment": match_payment, "stock-history": stock_history,
          "health-check": health_check, "uncleared": uncleared}


# ---- Frames.

def quoted(a: str) -> str:
    return f'"{a}"' if re.search(r"[\s,\"'&|<>]", a) else a


def text_rows(text: str, f: ImageFont.FreeTypeFont, width: float) -> list[str]:
    rows, row = [], ""
    for word in text.split():
        if row and f.getlength(f"{row} {word}") > width:
            rows.append(row)
            row = word
        else:
            row = f"{row} {word}" if row else word
    return rows + ([row] if row else [])


class Film:
    def __init__(self, steps: list[Step]):
        self.steps = steps
        self.ask_h = max(self.box_h(text_rows(s.ask, SANS_BOLD, W - 2 * PAD - 28), 25, 22) for s in steps if s.ask)
        self.term_y = 36 + 16 + self.ask_h + 16
        self.h = max(self.says_y(s) + self.says_h(s) for s in steps) + 18
        self.frames: list[Image.Image] = []
        self.durations: list[int] = []

    @staticmethod
    def box_h(rows: list[str], line: int, label: int) -> int:
        return 24 + label + len(rows) * line

    def says_y(self, s: Step) -> int:
        return self.term_y + len(self.term(s, len(s.out))) * LINE + 14

    def says_h(self, s: Step) -> int:
        return self.box_h(text_rows("✓ " + s.says, SANS, W - 2 * PAD - 28), 22, 0)

    def term(self, s: Step, shown: int, typed: int | None = None) -> list[list[tuple[str, str]]]:
        cmd = "qbd " + " ".join(quoted(a) for a in s.cmd)
        rows = wrapped([(s.note, DIM)]) if s.note else []
        rows += wrapped([("$ ", PROMPT), (cmd[:typed] if typed is not None else cmd, CMD)])
        for line in s.out[:shown] if typed is None else []:
            rows += wrapped(colored(line))
        return rows

    def frame(self, s: Step, ask: Step, rows: list[list[tuple[str, str]]], says: bool, ms: int) -> None:
        im = Image.new("RGB", (W, self.h), BG)
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, W, 36), fill=BAR)
        d.line((0, 36, W, 36), fill=BORDER)
        for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
            d.ellipse((16 + i * 20, 12, 28 + i * 20, 24), fill=c)
        title = "qbd --demo  ·  Bayside Hardware & Supply (sample company)"
        d.text(((W - SMALL.getlength(title)) / 2, 10), title, font=SMALL, fill=DIM)
        d.rounded_rectangle((PAD, 52, W - PAD, 52 + self.ask_h), 10, fill=ASK_BG)
        d.text((PAD + 14, 62), ask.asker, font=SMALL, fill=DIM)
        for i, row in enumerate(text_rows(ask.ask, SANS_BOLD, W - 2 * PAD - 28)):
            d.text((PAD + 14, 84 + i * 25), row, font=SANS_BOLD, fill=ASK)
        for i, row in enumerate(rows):
            x = PAD
            for text, color in row:
                d.text((x, self.term_y + i * LINE), text, font=MONO_BOLD if color == CMD else MONO, fill=color)
                x += len(text) * CHAR
        if says:
            y = self.says_y(s)
            d.rounded_rectangle((PAD, y, W - PAD, y + self.says_h(s)), 10, fill=SAYS_BG)
            for i, row in enumerate(text_rows("✓ " + s.says, SANS, W - 2 * PAD - 28)):
                d.text((PAD + 14, y + 12 + i * 22), row, font=SANS, fill=SAYS)
        self.frames.append(im)
        self.durations.append(ms)

    def shoot(self) -> None:
        ask = self.steps[0]
        for n, s in enumerate(self.steps):
            ask = s if s.ask else ask
            cmd = "qbd " + " ".join(quoted(a) for a in s.cmd)
            self.frame(s, ask, [], False, 1500 if s.ask else 400)
            per = max(2, math.ceil(len(cmd) / 30))
            for typed in range(per, len(cmd) + per, per):
                self.frame(s, ask, self.term(s, 0, min(typed, len(cmd))), False, 30)
            self.frame(s, ask, self.term(s, 0), False, 300)
            for shown in range(3, len(s.out) + 3, 3):
                self.frame(s, ask, self.term(s, shown), False, 45)
            self.frame(s, ask, self.term(s, len(s.out)), True, 5500 if n == len(self.steps) - 1 else 4000)

    def save(self, path: Path) -> None:
        # One palette for every frame, from the last frame plus a strip of the exact theme colours.
        theme = [BG, BAR, BORDER, TEXT, DIM, KEY, STR, NUM, PROMPT, CMD, ASK_BG, ASK, SAYS_BG, SAYS, "#ff5f56", "#ffbd2e", "#27c93f"]
        sample = Image.new("RGB", (W, self.h + 40), BG)
        sample.paste(self.frames[-1], (0, 0))
        for i, c in enumerate(theme):
            ImageDraw.Draw(sample).rectangle((i * 40, self.h, i * 40 + 39, self.h + 39), fill=c)
        palette = sample.quantize(colors=128)
        frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in self.frames]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=self.durations, loop=0)


def runner(folder: str) -> Callable:
    def run(args: list[str], writes: bool = False) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("QBD_") and k != "LOCALAPPDATA"}
        env.update(PYTHONPATH=str(ROOT / "src"), XDG_STATE_HOME=os.path.join(folder, "state"), PYTHONIOENCODING="utf-8")
        if writes:
            env.update(QBD_ALLOW_WRITES="true", QBD_WRITE_ENTITIES="receive_payment")
        p = subprocess.run([sys.executable, "-m", "qbd_agent_kit", "--demo", *args], cwd=folder, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            sys.exit(f"qbd {' '.join(args)} failed ({p.returncode}): {p.stdout}{p.stderr}")
        return json.loads(p.stdout)

    run.cwd = folder
    return run


def main() -> None:
    names = sys.argv[1:] or list(SCENES)
    for name in names:
        with tempfile.TemporaryDirectory() as folder:
            film = Film(SCENES[name](runner(folder)))
        film.shoot()
        path = ROOT / "assets" / f"{name}.gif"
        film.save(path)
        print(f"{path.relative_to(ROOT)}: {len(film.frames)} frames, {sum(film.durations) / 1000:.1f} s, "
              f"{W}x{film.h}, {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
