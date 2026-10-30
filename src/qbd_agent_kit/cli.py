"""qbd: QuickBooks Desktop for AI agents, run on the Windows PC that hosts QuickBooks.

Each command runs one tool and prints its answer as one line of JSON. A failure prints {"error": {...}} and exits
with 2 (change the command), 3 (a person must act at the PC) or 1 (anything else)."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import struct
import subprocess
import sys
import textwrap
import time
import traceback
from contextlib import nullcontext
from typing import Any

from . import __version__
from .bridge.com import ComTransport
from .config import Config, load_config
from .demo.transport import DemoTransport
from .errors import InvalidArguments, Paused, QbConnectionError, QbStatusError, QbxmlInputError, Stopped, ToolError, WriteUnconfirmed
from .jsutil import MISSING, dumps, num_str, text
from .qb.client import QbClient
from .qb.dates import local_today
from .safety.audit import calling_program, who_am_i, write_call
from .safety.live import Live, paused, remember_quickbooks, remembered, take_turn, who
from .tools import error_text, run_tool
from .tools.args import Arg
from .tools.common import Ctx, Drafts, Tool, company_facts
from .tools.files import FILE_TOOLS
from .tools.insights import INSIGHT_TOOLS
from .tools.read import READ_TOOLS
from .tools.write import WRITE_TOOLS

GROUPS = [("Workflows", INSIGHT_TOOLS), ("Records", READ_TOOLS), ("The QuickBooks PC", FILE_TOOLS), ("Changes (when QBD_ALLOW_WRITES=true)", WRITE_TOOLS)]
RENAMED = {"qb_inventory_reorder": "reorder", "qb_write_log": "log"}
COMMANDS = {RENAMED.get(t.name, t.name[3:].replace("_", "-")): t for _, tools in GROUPS for t in tools}
POSITIONAL = {
    "search": ["type"], "export": ["type"], "count": ["type"], "changes": ["since"], "coding": ["vendor"], "match-payment": ["customer", "amount"], "trace": ["ref"], "stock-history": ["item"], "reconcile-check": ["account"], "get": ["type", "id"], "report": ["report"], "schema": ["request"], "raw": ["request"],
    "draft": ["action", "type"], "import": ["type"], "commit": ["token"], "undo": ["token"], "addons": ["action", "addon"],
}
SETUP = {
    "doctor": "Check this PC and the connection to QuickBooks",
    "authorize": "Connect once so QuickBooks asks for permission (QuickBooks open, as Admin)",
}
META = {"int": "N", "date": "YYYY-MM-DD", "enum": "CHOICE", "str": "TEXT", "list": "A,B", "dict": "JSON|@FILE|-"}


class Usage(Exception):
    """A command line that cannot be run as typed."""


# ---------------------------------------------------------------- reading the command line


IDENTITY = {"--agent": "agent", "--session": "session", "--session-title": "session_title"}  # = QBD_AGENT, QBD_SESSION...


def split_globals(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    opts: dict[str, Any] = {"demo": False, "pretty": False, "version": False, "help": False, "out": None}
    rest: list[str] = []
    it = iter(argv)
    for a in it:
        flag, eq, value = a.partition("=")
        if a in ("--demo", "--pretty", "--version"):
            opts[a[2:]] = True
        elif a in ("--help", "-h"):
            opts["help"] = True
        elif flag == "--out":
            opts["out"] = value if eq else next(it, None)
            if not opts["out"]:
                raise Usage("--out needs a file name.")
        elif flag in IDENTITY:
            opts[IDENTITY[flag]] = value if eq else next(it, None)
            if not opts[IDENTITY[flag]]:
                raise Usage(f"{flag} needs a value.")
        else:
            rest.append(a)
    return opts, rest


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in JSON")


def read_json(flag: str, v: str) -> Any:
    """JSON given inline, as @file, or as - for standard input."""
    try:
        if v == "-":
            v = sys.stdin.buffer.read().decode("utf-8-sig")
        elif v.startswith("@"):
            with open(v[1:], encoding="utf-8-sig") as f:
                v = f.read()
    except OSError as e:
        raise Usage(f"Cannot read {v[1:]}: {e.strerror}.") from e
    try:
        return json.loads(v, parse_constant=_no_constant)
    except ValueError as e:
        raise Usage(f"{flag} must be JSON, inline or as @file: {e}.") from e


def read_value(flag: str, arg: Arg, v: str) -> Any:
    if arg.kind == "int":
        if re.fullmatch(r"-?[0-9]+", v):
            return int(v)
        try:
            f = float(v)
        except ValueError:
            return v  # the argument check says what is wrong
        return f if math.isfinite(f) else v
    if arg.kind == "dict":
        return read_json(flag, v)
    return v


def tool_args(t: Tool, positional: list[str], tokens: list[str]) -> dict[str, Any]:
    names = {"--" + k.replace("_", "-"): k for k in t.input}
    args: dict[str, Any] = {}
    loose: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token.startswith("--"):
            loose.append(token)
            continue
        flag, eq, value = token.partition("=")
        name = names.get(flag)
        if name is None:
            raise Usage(f"Unknown option {flag}.")
        arg = t.input[name]
        if arg.kind == "bool":
            args[name] = {"true": True, "false": False}.get(value.lower(), value) if eq else True
            continue
        if not eq:
            if i >= len(tokens) or tokens[i].startswith("--"):
                raise Usage(f"{flag} needs a value.")
            value = tokens[i]
            i += 1
        if arg.kind == "list":
            args.setdefault(name, []).extend(x.strip() for x in value.split(",") if x.strip())
        else:
            args[name] = read_value(flag, arg, value)
    if len(loose) > len(positional):
        raise Usage(f"Unexpected argument {loose[len(positional)]!r}.")
    for name, v in zip(positional, loose):
        args[name] = read_value(f"<{name}>", t.input[name], v)
    return args


# ---------------------------------------------------------------- help


def usage(cmd: str) -> str:
    t = COMMANDS[cmd]
    return " ".join([f"qbd {cmd}", *(f"<{p}>" if t.input[p].required else f"[<{p}>]" for p in POSITIONAL.get(cmd, []))])


def _default(v: Any) -> str:
    return ",".join(v) if isinstance(v, list) else text(v)


def describe_arg(a: Arg) -> str:
    parts = [a.help.rstrip(".")] if a.help else []
    if a.choices and not all(c in a.help for c in a.choices):  # some help texts already list every choice
        parts.append(("comma-separated, from: " if a.kind == "list" else "one of: ") + ", ".join(a.choices))
    if a.min is not None and a.max is not None:
        parts.append(f"{a.min} to {a.max}")
    if a.required:
        parts.append("required")
    elif a.default is not MISSING and a.default not in ({}, False):
        parts.append(f"default {_default(a.default)}")
    return "; ".join(parts)


def command_help(cmd: str) -> str:
    t = COMMANDS[cmd]
    pos = POSITIONAL.get(cmd, [])
    rows = [
        (f"<{k}>" if k in pos else "--" + k.replace("_", "-") + ("" if a.kind == "bool" else f" {META[a.kind]}"), describe_arg(a))
        for k, a in t.input.items()
    ]
    out = [f"Usage: {usage(cmd)}{' [options]' if len(rows) > len(pos) else ''}", "", f"{t.title}.", *textwrap.wrap(t.description, 100)]
    if rows:
        width = min(max(len(r[0]) for r in rows) + 2, 28)
        out.append("")
        for label, about in rows:
            lines = textwrap.wrap(about, 100 - width) or [""]
            out.append(f"  {label:<{width}}{lines[0]}" if len(label) < width else f"  {label}\n  {'':<{width}}{lines[0]}")
            out += [f"  {'':<{width}}{x}" for x in lines[1:]]
    return "\n".join(out)


def general_help() -> str:
    out = [
        f"qbd {__version__}: QuickBooks Desktop for AI agents, run on the Windows PC that hosts QuickBooks.",
        "",
        "Usage: qbd <command> [arguments] [options]",
        "",
        'Each command prints one line of JSON. A failure prints {"error": {"code", "message", "hint", "retryable"}}',
        "and exits with 2 (change the command), 3 (a person must act at the PC) or 1 (anything else).",
    ]
    for title, tools in GROUPS:
        out += ["", title]
        out += [f"  {usage(c):<26}  {t.title}" for c, t in COMMANDS.items() if t in tools]
    out += ["", "Setup", *(f"  {f'qbd {c}':<26}  {about}" for c, about in SETUP.items())]
    out += [
        "",
        "Options",
        "  --demo                      use the built-in sample company instead of QuickBooks",
        "  --pretty                    indent the JSON",
        "  --out FILE                  write the JSON to FILE as UTF-8 (in Windows PowerShell 5.1, > writes UTF-16)",
        "  --version                   print the version",
        "",
        "Run qbd help <command> for its options. Settings are environment variables (QBD_ALLOW_WRITES,",
        "QBD_COMPANY_FILE, ...); see the README.",
    ]
    return "\n".join(out)


def help_text(cmd: str) -> str:
    if cmd in COMMANDS:
        return command_help(cmd)
    if cmd in SETUP:
        return f"Usage: qbd {cmd} [--demo]\n\n{SETUP[cmd]}."
    return general_help()


# ---------------------------------------------------------------- running a tool


def make_context(config: Config, open_timeout: float | None = None) -> Ctx:
    transport = (
        DemoTransport(local_today())
        if config.demo
        else ComTransport(config.app_id, config.app_name, config.company_file, open_timeout or config.open_timeout_seconds, config.timeout_seconds)
    )
    drafts = Drafts(os.path.join(config.state_dir, "drafts"))
    return Ctx(qb=QbClient(transport, config.qbxml_version), config=config, today=local_today, drafts=drafts, cache={})


def with_identity(config: Config, opts: dict[str, Any]) -> Config:
    """Who runs this command: the assistant's name and session from the options or QBD_AGENT, QBD_SESSION and
    QBD_SESSION_TITLE; without a name, the program that started qbd."""
    for k in IDENTITY.values():
        if opts.get(k):
            setattr(config, k, re.sub(r"\s+", " ", opts[k]).strip()[: 60 if k == "agent" else 80])
    if not config.agent:
        config.program = calling_program()
    return config


def identity(config: Config) -> dict[str, str]:
    """The call log's and live file's fields for who ran a command."""
    out = {"agent": config.agent} if config.agent else {"program": config.program} if config.program else {}
    return {**out, **({"session": config.session} if config.session else {}),
            **({"session_title": config.session_title} if config.session_title else {})}


def company(ctx: Ctx | None, config: Config) -> dict[str, str]:
    """The company file a command worked in: the one QuickBooks opened, else the one it was bound to; and its name,
    once qbd has read it."""
    path = (ctx.qb.company_file if ctx else None) or config.company_file
    if not path:
        return {}
    facts = ctx.cache.get(f"facts:{path}") if ctx else None
    name = facts.name if facts is not None else remembered(config.state_dir, config.demo, path).get("company")
    return {**({"company": name} if name else {}), "company_file": path}


def remember(ctx: Ctx, config: Config) -> None:
    """What qbd read about the company file this time, for the Activity Monitor's Safety panel."""
    host = ctx.qb.host
    if host is None or not host.company_file:
        return
    facts = ctx.cache.get(f"facts:{host.company_file}")
    remember_quickbooks(config.state_dir, config.demo, host.company_file, {
        "product": host.product_name, "version": host.version, "country": host.country, "qbxml": host.qbxml_version,
        **({"company": facts.name, "closing_date": facts.closing_date} if facts is not None else {}),
        "by": config.agent or config.program or "",
    })


def failure(e: BaseException, cmd: str) -> tuple[dict, int]:
    code, message, hint, status = "internal", f"{type(e).__name__}: {e}", None, 1
    if isinstance(e, (Usage, InvalidArguments)):
        code, message, status = "invalid_arguments", error_text(e), 2
        hint = f"Run: qbd help {cmd}" if cmd in COMMANDS else "Run: qbd help"
    elif isinstance(e, QbxmlInputError):
        code, message, status = "invalid_request", error_text(e), 2
    elif isinstance(e, ToolError):
        code, message, status = "refused", str(e), 2
    elif isinstance(e, WriteUnconfirmed):
        code, message = "unconfirmed", str(e)
    elif isinstance(e, Stopped):
        code, message, status = "stopped", str(e), 3
        hint = "Tell the person. This session's next commands wait until they click Resume in the Activity Monitor."
    elif isinstance(e, Paused):
        code, message, hint, status = "paused", str(e), e.hint, 3
    elif isinstance(e, QbConnectionError):
        code, message, hint, status = e.code, e.message, e.hint, 3 if e.needs_person else 1
    elif isinstance(e, QbStatusError):
        code, message, hint = f"qb_status_{num_str(e.status_code)}", e.message, e.hint
    else:
        traceback.print_exc()
    # A change that timed out may still be saved: it is never repeated blindly.
    retryable = code in ("timeout", "busy") and cmd != "commit"
    return {"error": {"code": code, "message": message, **({"hint": hint} if hint else {}), "retryable": retryable}}, status


def emit(value: Any, opts: dict[str, Any]) -> int:
    out = dumps(value, pretty=opts["pretty"])
    if opts["out"]:
        with open(opts["out"], "w", encoding="utf-8", newline="\n") as f:
            f.write(out + "\n")
    else:
        print(out)
    return len(out)


def short(v: str) -> str:
    return v if len(v) <= 120 else v[:120] + "…"


def call_args(args: dict[str, Any], positional: list[str]) -> dict[str, Any]:
    """A command's arguments for the call log, the positional ones first. A record passed in (--fields) shows only its
    field names."""
    out: dict[str, Any] = {}
    for k, v in sorted(args.items(), key=lambda kv: kv[0] not in positional):
        if isinstance(v, dict):
            out[k] = sorted(v)
        elif isinstance(v, list) and any(isinstance(x, dict) for x in v):
            out[k] = f"{len(v)} records"
        else:
            out[k] = short(v) if isinstance(v, str) else v
    return out


def log_call(config: Config, cmd: str, given: Any, outcome: dict[str, Any], started: float, ctx: Ctx | None, size: int) -> None:
    """One line in the call log for each command: who ran it, in which company file, with which settings, and what it
    asked QuickBooks. Failing to write it never fails the command."""
    requests = ctx.qb.calls if ctx else []
    t = COMMANDS.get(cmd)
    try:
        entry = {**identity(config), **company(ctx, config), "settings": config.settings(), "command": cmd, **({"title": t.title} if t else {}),
                 "args": given, **outcome, "ms": round((time.monotonic() - started) * 1000), "items": sum(r.get("items", 0) for r in requests),
                 "output_bytes": size, "requests": requests}
        write_call(config.call_log, entry)
    except Exception:  # noqa: BLE001 - the answer is already printed; a failed commit must not look like a failed command
        pass


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    opts: dict[str, Any] = {"pretty": False, "out": None}
    cmd = ""
    transport: Any = None
    config: Config | None = None
    ctx: Ctx | None = None
    live: Live | None = None
    given: Any = None  # the arguments as the call log shows them
    started = time.monotonic()
    try:
        opts, rest = split_globals(sys.argv[1:] if argv is None else argv)
        cmd = rest[0] if rest else ""
        if opts["version"]:
            print(__version__)
            return 0
        if opts["help"] or cmd in ("", "help"):
            print(help_text(rest[1] if cmd == "help" and len(rest) > 1 else cmd))
            return 0
        config = with_identity(load_config(demo=True) if opts["demo"] else load_config(), opts)
        if cmd == "doctor":
            return doctor(config)
        if cmd == "authorize":
            return authorize(config)
        given = [short(x) for x in rest[1:]]
        t = COMMANDS.get(cmd)
        if t is None:
            raise Usage(f"Unknown command {cmd}.")
        if t.writes and not config.allow_writes:
            raise ToolError("Changes are turned off on this PC (QBD_ALLOW_WRITES is not true). The owner can turn them on; see the README.")
        args = tool_args(t, POSITIONAL.get(cmd, []), rest[1:])
        given = call_args(args, POSITIONAL.get(cmd, []))
        ctx = make_context(config)
        transport = ctx.qb.transport
        # The sample company lives in this process; schema, activity and addons work on the kit's own files.
        if config.demo or cmd in ("schema", "activity", "addons"):
            turn: Any = nullcontext()
        else:
            key = who(config.agent, config.session, config.program)
            held = paused(config.state_dir, key)
            if held is not None:
                raise Paused(str(held.get("by") or "The person at this PC"), str(held.get("at") or ""))
            live = Live(config.state_dir, {**key, **identity(config), "user": who_am_i(), "command": cmd, "title": t.title, "args": given})
            live.name_of = lambda path: remembered(config.state_dir, config.demo, path).get("company")
            ctx.live = ctx.qb.watch = live
            turn = take_turn(config.state_dir, live, min(config.timeout_seconds, 60))
        with turn:
            try:
                result = run_tool(ctx, t, args)
            finally:
                transport.close()
                remember(ctx, config)
        size, outcome = emit(result, opts), {"outcome": "ok"}
        status = 0
    except (Exception, Stopped) as e:  # noqa: BLE001 - every failure becomes a JSON error
        body, status = failure(e, cmd)
        out = dumps(body, pretty=opts["pretty"])
        print(out)
        size, outcome = len(out), {"outcome": body["error"]["code"], "error": body["error"]["message"],
                                   **({"hint": body["error"]["hint"]} if "hint" in body["error"] else {})}
    finally:
        if live is not None:
            live.end()
    if config is not None and config.call_log and given is not None:
        log_call(config, cmd, given, outcome, started, ctx, size)
    if getattr(transport, "stuck", False):
        # A request is still running inside QuickBooks on a thread that cannot be stopped: leave without waiting for it.
        sys.stdout.flush()
        os._exit(status)
    return status


# ---------------------------------------------------------------- setup commands


def line(ok: bool | None, msg: str) -> None:
    print(f"{' - ' if ok is None else 'OK ' if ok else '!! '} {msg}")


def sdk_registrations() -> dict[str, str]:
    """Where the QuickBooks SDK component is registered: {"64-bit": dll, "32-bit": dll} and/or {"any": exe}."""
    import winreg

    found: dict[str, str] = {}
    for bits, view in (("64-bit", winreg.KEY_WOW64_64KEY), ("32-bit", winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"QBXMLRP2.RequestProcessor\CLSID", 0, winreg.KEY_READ | view) as k:
                clsid = winreg.QueryValueEx(k, "")[0]
        except OSError:
            continue
        for server, key in (("InprocServer32", bits), ("LocalServer32", "any")):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\{server}", 0, winreg.KEY_READ | view) as k:
                    found[key] = winreg.QueryValueEx(k, "")[0]
            except OSError:
                pass
    return found


def quickbooks_running() -> bool | None:
    try:
        out = subprocess.run(
            # errors="replace": tasklist writes in the console code page, which is not UTF-8 (Python 3.15's default).
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None  # only a hint
    return re.search(r'"QBW(32)?\.EXE"', out, re.I) is not None


def doctor(config: Config) -> int:
    bits = f"{struct.calcsize('P') * 8}-bit"
    print(f"qbd {__version__} doctor\n")
    line(True, f"Python {platform.python_version()} ({bits}) on {platform.platform()}")
    if not config.demo:
        if sys.platform != "win32":
            line(False, "QuickBooks Desktop runs on Windows. On this computer only --demo works.")
            return 1
        try:
            import win32com.client  # noqa: F401

            line(True, "pywin32 is installed")
        except ImportError:
            line(False, "pywin32 is missing. Install it with: python -m pip install pywin32")
            return 1
        found = sdk_registrations()
        for b in ("64-bit", "32-bit"):
            line(True if b in found else None, f"{b} QuickBooks SDK: {f'registered ({found[b]})' if b in found else 'not registered'}")
        if found and bits not in found and "any" not in found:
            other = next(iter(found))
            line(False, f"This Python is {bits}, but the QuickBooks SDK is registered for {other} programs only. Run qbd with a {other} Python from python.org.")
        running = quickbooks_running()
        if running is not None:
            line(True if running else None, "QuickBooks is running" if running else "QuickBooks is not running (needed unless the app may log in without it)")
    line(None, f'App name shown in QuickBooks: "{config.app_name}"')
    if config.allow_writes:
        line(None, f"Writes: allowed with approval ({', '.join(config.write_entities)}; up to {num_str(config.max_amount)}{'; delete/void on' if config.allow_delete else ''})")
    else:
        line(None, "Writes: off (read-only)")
    line(None, f"Audit log: {config.audit_log}")
    line(None, f"Call log: {config.call_log or 'off (QBD_CALL_LOG)'}")
    ctx = make_context(config, open_timeout=min(config.open_timeout_seconds, 45))
    try:
        host = ctx.qb.host_info()
        facts = company_facts(ctx)
        line(True, f"Connected: {facts.name} ({host.company_file})")
        line(True, f"{host.product_name} {host.version}, qbXML {host.qbxml_version}{f', {host.country}' if host.country else ''}")
        line(True if facts.closing_date else None, f"Closing date: {facts.closing_date or 'not set'}")
        probe = ctx.qb.query("CustomerQueryRq", {"ActiveStatus": "ActiveOnly", "IncludeRetElement": ["ListID"]}, 1)
        line(True, f"Read test: {'customers can be read' if probe.rows else 'no customers yet, but the query worked'}")
        print("\nAll good. Try: qbd daily-brief --pretty")
        return 0
    except Exception as e:  # noqa: BLE001 - reported to the person
        line(False, error_text(e))
        print("\nIf QuickBooks shows a permission dialog or this is the first run, use: qbd authorize")
        return 1
    finally:
        ctx.qb.transport.close()


def authorize(config: Config) -> int:
    if config.demo:
        print("The demo company needs no permission.")
        return 0
    print(f"""Connecting to QuickBooks as "{config.app_name}".

The first time, QuickBooks shows an "Application permission" dialog (it may open behind other windows):
  1. Choose "Yes, whenever my QuickBooks company file is open"
     (or "Yes, always allow access; even when my QuickBooks isn't running" and pick the user to log in as).
  2. Leave "Allow this application to access personal data" unticked unless you need it.
  3. Type yes in the confirmation box if QuickBooks asks, then Continue and Done.
QuickBooks must be open with the company file, logged in as the Admin user.

Waiting up to 10 minutes...""", flush=True)
    ctx = make_context(config, open_timeout=600)
    try:
        host = ctx.qb.host_info()
        facts = company_facts(ctx)
        print(f'\nDone: "{config.app_name}" can read {facts.name} ({host.company_file}).')
        print("To change or remove this permission later: QuickBooks > Edit > Preferences > Integrated Applications > Company Preferences.")
        return 0
    except Exception as e:  # noqa: BLE001 - reported to the person
        print(f"\n{error_text(e)}")
        return 1
    finally:
        ctx.qb.transport.close()
