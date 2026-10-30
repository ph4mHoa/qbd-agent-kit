"""What an AI agent did on this PC, for the person who checks it: the call log (every command) and the write log
(every change) as a short page any web browser opens, and as JSON for agents. The page gives one line per item and
leaves the logs' details out. It is plain HTML and CSS with no scripts, written to the state folder; nothing leaves
the PC."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import webbrowser
from pathlib import Path
from typing import Any

from ..safety.audit import read_log
from .args import Arg
from .common import Ctx, tool

ROWS = 1000  # commands shown on the page at most
# outcome code -> (label, colour); "ok" is no problem, anything not listed is a failure
PROBLEMS = {"refused": ("Refused", "warn"), "invalid_arguments": ("Wrong request", "warn"),
            "invalid_request": ("Wrong request", "warn"), "busy": ("Busy", "warn")}
SAVED = {"done": ("Saved", "ok"), "failed": ("Not saved", "bad"), "unknown": ("Check it", "warn"), "rejected": ("Rejected", "warn")}
# what a change command did, in place of a record count
DID = {"draft": "Previewed, not saved yet", "import": "Previewed, not saved yet", "undo": "Undo previewed, not saved yet",
       "commit": "Saved to QuickBooks"}
HIDDEN_ARGS = {"fields", "body", "reason", "token", "limit"}  # shown elsewhere on the page, or no help to the reader
CALL_KEYS = ("time", "command", "title", "args", "outcome", "error", "ms", "items")  # what the JSON answer keeps

STYLE = """
:root { --bg: #f6f7f9; --card: #fff; --ink: #1d2433; --muted: #5b6475; --line: #e3e6ec;
  --ok: #1a7f4b; --ok-bg: #e3f4ea; --warn: #8a5a00; --warn-bg: #fdf1d8; --bad: #b42318; --bad-bg: #fde7e5; }
@media (prefers-color-scheme: dark) { :root { --bg: #14171c; --card: #1d2128; --ink: #e8eaee; --muted: #9aa3b2;
  --line: #2c323c; --ok: #6fd39b; --ok-bg: #173526; --warn: #f0c060; --warn-bg: #3a2f14; --bad: #ff8f84; --bad-bg: #3d1c1a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
main { max-width: 1000px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 4px; } h2 { font-size: 19px; margin: 32px 0 10px; } h3 { font-size: 15px; margin: 22px 0 8px; color: var(--muted); }
.sub, footer { color: var(--muted); } footer { margin-top: 36px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 20px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.card b { display: block; font-size: 26px; } .card span { color: var(--muted); font-size: 13px; }
.wrap { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; border-collapse: collapse; } th, td { text-align: left; vertical-align: top; padding: 9px 12px; border-top: 1px solid var(--line); }
th { border-top: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
td.time, td.num { white-space: nowrap; } td.num { text-align: right; }
td small { display: block; color: var(--muted); }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
.ok { color: var(--ok); background: var(--ok-bg); } .warn { color: var(--warn); background: var(--warn-bg); } .bad { color: var(--bad); background: var(--bad-bg); }
.empty { background: var(--card); border: 1px dashed var(--line); border-radius: 10px; padding: 14px 16px; color: var(--muted); }
"""


def _when(e: dict) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(e.get("time")), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc).astimezone()
    except ValueError:
        return None


def _secs(ms: Any) -> str:
    s = (ms if isinstance(ms, (int, float)) else 0) / 1000
    if s < 0.01:
        return "under 0.01 s"
    return f"{s:.2f} s" if s < 1 else f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {round(s % 60)} s"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def details(e: dict) -> str:
    """What a command was about, in a few words: names and types as given, dates and numbers with their label."""
    args = e.get("args")
    if isinstance(args, list):  # the command line could not be read: what was typed
        return _cut(" ".join(map(str, args)), 60)
    bits = []
    for k, v in (args.items() if isinstance(args, dict) else []):
        if k in HIDDEN_ARGS or isinstance(v, (bool, dict)) or v is None:
            continue
        if isinstance(v, list):
            v = ", ".join(map(str, v))
        if isinstance(v, str) and not re.fullmatch(r"[\d.,-]+", v):
            bits.append(v.replace("_", " ") if re.fullmatch(r"[a-z0-9_]+", v) else v)
        else:
            bits.append(f"{k.replace('_', ' ')} {v}")
    return _cut(" · ".join(bits), 60)


def _first_sentence(s: Any) -> str:
    return _cut(re.split(r"(?<=\.)\s", str(s or ""), maxsplit=1)[0], 120)


def _badge(label: str, colour: str) -> str:
    return f'<span class="badge {colour}">{html.escape(label)}</span>'


def _result(e: dict) -> str:
    """A command's result in a few words, or a badge when it went wrong."""
    if e.get("outcome") != "ok":
        return _badge(*PROBLEMS.get(str(e.get("outcome")), ("Failed", "bad")))
    if e.get("command") in DID:
        return html.escape(DID[e["command"]])
    if not e.get("requests"):
        return "Done"
    n = e.get("items", 0)
    return f"{n:,} record{'' if n == 1 else 's'}"


def _what(e: dict) -> str:
    about = details(e)
    return html.escape(str(e.get("title") or f"qbd {e.get('command', '?')}")) + (f"<small>{html.escape(about)}</small>" if about else "")


def _call_row(e: dict, when: dt.datetime) -> str:
    return (f'<tr><td class="time">{when:%H:%M}</td><td>{_what(e)}</td><td>{_result(e)}</td>'
            f'<td class="num">{_secs(e.get("ms"))}</td></tr>')


def _problem_row(e: dict, when: dt.datetime) -> str:
    return (f'<tr><td class="time">{when:%d %b %H:%M}</td><td>{_what(e)}</td>'
            f'<td>{html.escape(_first_sentence(e.get("error")))}</td><td>{_result(e)}</td></tr>')


def _change_row(e: dict, when: dt.datetime) -> str:
    label, colour = SAVED.get(str(e.get("outcome")), ("Check it", "warn"))
    change = str(e.get("summary", "")).split("\n")[0]
    why_not = _first_sentence(e.get("error")) if e.get("outcome") != "done" else ""
    reason = e.get("reason") if isinstance(e.get("reason"), str) else ""
    return (f'<tr><td class="time">{when:%d %b %H:%M}</td><td>{html.escape(change)}'
            f'{f"<small>{html.escape(why_not)}</small>" if why_not else ""}</td>'
            f'<td>{html.escape(reason)}</td><td>{_badge(label, colour)}</td></tr>')


def _table(head: list[str], rows: list[str], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{html.escape(empty)}</p>'
    return f'<div class="wrap"><table><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr>{"".join(rows)}</table></div>'


def render(calls: list[tuple[dt.datetime, dict]], changes: list[tuple[dt.datetime, dict]], totals: dict, days: int,
           now: dt.datetime, folder: str, call_log_on: bool, more: int) -> str:
    """The page. calls and changes: newest first, within the period."""
    by_day: dict[str, list[str]] = {}
    for w, e in calls:
        by_day.setdefault(f"{w:%A %d %B}", []).append(_call_row(e, w))
    cards = [(totals["changes_saved"], "changes saved"), (totals["commands"], "requests by the AI"),
             (f"{totals['records_read']:,}", "records read"), (totals["needs_a_look"], "need a look")]
    if not call_log_on:
        asked = '<p class="empty">The call log is turned off on this PC (QBD_CALL_LOG), so only changes are shown.</p>'
    elif not calls:
        asked = '<p class="empty">No requests in this period.</p>'
    else:
        asked = "".join(f"<h3>{html.escape(day)}</h3>" + _table(["Time", "Request", "Result", "Took"], rows, "")
                        for day, rows in by_day.items())
        asked += f'<p class="sub">The newest {ROWS:,} are shown; {more:,} older ones are in the call log.</p>' if more else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI activity in QuickBooks</title><style>{STYLE}</style></head>
<body><main>
<h1>What the AI did in QuickBooks</h1>
<p class="sub">The last {days} day{'' if days == 1 else 's'} on this PC, as of {now:%d %B %Y, %H:%M}.</p>
<div class="cards">{"".join(f'<div class="card"><b>{html.escape(str(v))}</b><span>{t}</span></div>' for v, t in cards)}</div>
<h2>Changes to your books</h2>
{_table(["When", "Change", "Reason given", "Result"], [_change_row(e, w) for w, e in changes], "No changes were saved or tried in this period.")}
<h2>Needs a look</h2>
{_table(["When", "Request", "Why", ""], [_problem_row(e, w) for w, e in calls if e.get("outcome") != "ok"], "Nothing went wrong in this period.")}
<h2>Everything the AI asked for</h2>
{asked}
<footer>From the logs in {html.escape(folder)} on this PC; nothing here leaves it. To refresh, run
<code>qbd activity --open</code> again.</footer>
</main></body></html>
"""


@tool(
    "qb_activity",
    "What the AI did: a page to check it",
    "Everything run through qbd on this PC in the last days: each command an agent or person ran (what was asked, how "
    "long it took, how many records QuickBooks sent back, what went wrong) and each change saved or tried. Writes a page "
    "any web browser opens, for the person who checks the agent's work, and returns the newest entries as JSON. Run it "
    "with --open when the person asks what you did, or at the end of a session.",
    {
        "days": Arg("int", "How many days the page covers", default=7, min=1, max=90),
        "limit": Arg("int", "How many commands and changes the JSON answer lists", default=20, min=1, max=500),
        "open": Arg("bool", "Open the page in the web browser"),
    },
)
def activity(ctx: Ctx, a: dict) -> dict:
    cfg = ctx.config
    now = dt.datetime.now().astimezone()
    since = now - dt.timedelta(days=a["days"])

    def recent(path: str) -> list[tuple[dt.datetime, dict]]:
        out = []
        for e in reversed(read_log(path, since.timestamp())) if path else []:
            w = _when(e)
            if w is not None and w >= since and e.get("command") != "activity":  # opening this page is not work in the books
                out.append((w, e))
        return out

    calls, changes = recent(cfg.call_log), recent(cfg.audit_log)
    totals = {
        "commands": len(calls),
        "changes_saved": sum(1 for _, e in changes if e.get("outcome") == "done"),
        "records_read": sum(e.get("items", 0) for _, e in calls if isinstance(e.get("items"), int)),
        "needs_a_look": sum(1 for _, e in calls if e.get("outcome") != "ok") + sum(1 for _, e in changes if e.get("outcome") in ("failed", "unknown")),
        "quickbooks_ms": sum(r.get("ms", 0) for _, e in calls for r in e.get("requests") or [] if isinstance(r, dict)),
    }
    page = os.path.join(cfg.state_dir, "activity-demo.html" if cfg.demo else "activity.html")
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(render(calls[:ROWS], changes, totals, a["days"], now, cfg.state_dir, bool(cfg.call_log), max(0, len(calls) - ROWS)))
    out: dict[str, Any] = {"page": page}
    if a["open"]:
        out["opened"] = webbrowser.open(Path(page).resolve().as_uri())
    keep = ("time", "user", "action", "entity", "summary", "reason", "outcome", "ids", "error")
    return {
        **out,
        "days": a["days"],
        "totals": totals,
        "changes": [{k: e[k] for k in keep if k in e} for _, e in changes[: a["limit"]]],
        "calls": [{k: e[k] for k in CALL_KEYS if k in e} for _, e in calls[: a["limit"]]],
        **({} if cfg.call_log else {"note": "The call log is off on this PC (QBD_CALL_LOG), so only changes are listed."}),
    }
