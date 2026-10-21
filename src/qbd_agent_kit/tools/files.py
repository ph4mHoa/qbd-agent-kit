"""The QuickBooks PC itself: company file, transaction log, backups, QuickBooks' own log, services and free disk.
Read-only and from the file system, so it also works while QuickBooks is busy when the company file is given."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..jsutil import MISSING, round2
from ..qb.dates import add_days, days_between
from .args import Arg
from .common import Ctx, tool

# trackdll.cpp (148) :  MESSAGE: 4704: Tue Oct 20 04:26:18 LVL_SEVERE_ERROR--MainFrame must be already created...
LOG_LINE = re.compile(r"^\S+ \(\d+\) :\s+[A-Z_ ]+?: \d+: \w{3} (\w{3}) +(\d+) (\d\d:\d\d:\d\d) (.*)$")
LEVEL = re.compile(r"\bLVL_(FATAL_ERROR|SEVERE_ERROR|ERROR|WARNING)--")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LOG_TAIL = 16 * 1024 * 1024  # the newest part of QBWin.log that is read
SERVICES = re.compile(r"QuickBooks|QBCF|QBFC|QBWeb|Intuit", re.I)


def _mb(n: float) -> float:
    return round2(n / 1024 / 1024)


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _newest(paths: list[Path]) -> Path | None:
    return max(paths, key=lambda p: p.stat().st_mtime, default=None)


def _walk(folder: Path, suffix: str, depth: int) -> list[Path]:
    found: list[Path] = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.name.lower().endswith(suffix):
                found.append(p)
            elif p.is_dir() and depth > 0:
                found += _walk(p, suffix, depth - 1)
    except OSError:
        pass
    return found


def log_time(month: str, day: str, clock: str, today: str) -> str:
    """QBWin.log leaves out the year: it is this year, or last year for a date after today."""
    year, m, d = int(today[:4]), MONTHS.index(month) + 1, int(day)
    if (m, d) > (int(today[5:7]), int(today[8:10])):
        year -= 1
    return f"{year:04d}-{m:02d}-{d:02d}T{clock}"


def scan_log(text: str, today: str, since: str) -> dict:
    """Fatal, severe and other errors QuickBooks logged since a date, the same message counted once, newest first;
    and the last lines about verifying the data."""
    counts = {"fatal": 0, "severe": 0, "error": 0, "warning": 0}
    seen: dict[str, dict] = {}
    verify: list[dict] = []
    first = last = MISSING
    for line in text.splitlines():
        m = LOG_LINE.match(line)
        if not m or m.group(1) not in MONTHS:
            continue
        when = log_time(m.group(1), m.group(2), m.group(3), today)
        first, last = first if first is not MISSING else when, when
        message = m.group(4)
        level = LEVEL.search(message)
        if level and when >= since:
            kind = level.group(1).split("_")[0].lower()
            counts[kind] += 1
            text_ = message[level.end():].strip()[:200]
            key = re.sub(r"\d+", "#", text_)
            seen[key] = {"level": kind, "message": text_, "last": when, "times": seen.get(key, {}).get("times", 0) + 1}
        elif re.search(r"verif", message, re.I) and re.search(r"integrity|problem|fail|error|complete", message, re.I):
            verify.append({"time": when, "line": message.strip()[:200]})
    problems = sorted(seen.values(), key=lambda p: p["last"], reverse=True)
    return {"from": first, "to": last, "since": since, "counts": counts, "recent": problems[:10], "verify": verify[-3:]}


def qbwin_logs() -> list[Path]:
    """QuickBooks writes QBWin.log under the user's local app data (one folder per version since 2019) or ProgramData."""
    roots = [Path(os.environ[k]) / "Intuit" for k in ("LOCALAPPDATA", "ProgramData") if os.environ.get(k)]
    found = [p for r in roots if r.is_dir() for p in r.glob("QuickBooks*/**/QBWin.log")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def on_network(path: str) -> Any:
    if path.startswith("\\\\"):
        return True
    if sys.platform != "win32":
        return MISSING
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(path)[0] + "\\") == 4  # DRIVE_REMOTE


def quickbooks_services() -> Any:
    try:
        import win32service
    except ImportError:
        return MISSING
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE)
    try:
        listed = win32service.EnumServicesStatus(scm, win32service.SERVICE_WIN32, win32service.SERVICE_STATE_ALL)
    finally:
        win32service.CloseServiceHandle(scm)
    return [{"name": name, "running": status[1] == win32service.SERVICE_RUNNING} for name, _, status in listed if SERVICES.search(name)]


@tool(
    "qb_file_health",
    "Company file and QuickBooks PC health",
    "Read-only checks on the PC that holds the company file: file and transaction log (.TLG) size, whether the file "
    "is opened over the network, Auto Data Recovery copies, the newest .QBB backup, errors in QuickBooks' own log "
    "(QBWin.log) and its last lines about verifying data, QuickBooks services, and free disk. Use it when QuickBooks "
    "is slow, shows an error, or before a Rebuild, Condense or upgrade.",
    {
        "file": Arg("str", "Path of the company file (.QBW); the one open in QuickBooks when left out"),
        "backup_dir": Arg("str", "Another folder to search for .QBB backups"),
        "log_days": Arg("int", "Errors from QuickBooks' log for this many days", default=7, min=1, max=365),
    },
)
def file_health(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    path = a["file"] or ctx.qb.host_info().company_file
    qbw = Path(path)
    warnings: list[str] = []
    out: dict[str, Any] = {"as_of": today}
    if not qbw.is_file():
        out["file"] = {"path": path, "found": False}
        warnings.append("The company file is not on this computer: run this on the PC that holds it, or pass its path as file.")
    else:
        size = qbw.stat().st_size
        tlg = _newest([p for p in (Path(f"{path}.TLG"), qbw.with_suffix(".TLG")) if p.is_file()])
        adr = _newest(_walk(qbw.parent / "QuickBooksAutoDataRecovery", ".adr", 0))
        out["file"] = {
            "path": path, "size_mb": _mb(size), "modified": _stamp(qbw.stat().st_mtime), "on_network_drive": on_network(path),
            **({"tlg_mb": _mb(tlg.stat().st_size)} if tlg else {}),
            **({"auto_recovery_copy": _stamp(adr.stat().st_mtime)} if adr else {}),
        }
        if tlg and tlg.stat().st_size > max(size, 100 * 1024 ** 2):
            warnings.append("The transaction log (.TLG) is bigger than the company file: a manual backup (File > Back Up Company) starts a new one.")
        free = shutil.disk_usage(qbw.parent).free
        out["disk"] = {"free_gb": round2(free / 1024 ** 3), "total_gb": round2(shutil.disk_usage(qbw.parent).total / 1024 ** 3)}
        if free < size:
            warnings.append("Less free disk space than the size of the company file: a backup or Rebuild Data may fail.")
        folders = [qbw.parent, *([Path(a["backup_dir"])] if a["backup_dir"] else []),
                   *(Path(os.environ[k]) / "Documents" for k in ("USERPROFILE", "PUBLIC") if os.environ.get(k))]
        backups = [p for f in folders for p in _walk(f, ".qbb", 2)]
        best = _newest(backups)
        out["backups"] = {"searched": [str(f) for f in folders], **({"newest": {"path": str(best), "made": _stamp(best.stat().st_mtime),
                                                                              "size_mb": _mb(best.stat().st_size)}} if best else {})}
        if not best or best.stat().st_mtime < time.time() - 7 * 86400:
            warnings.append("No .QBB backup from the last 7 days was found here. If backups go to another folder, pass it as backup_dir.")
    logs = qbwin_logs()
    if logs:
        log = logs[0]
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - LOG_TAIL))
            text = f.read().decode("utf-8", errors="replace")
        scan = scan_log(text, today, add_days(today, -a["log_days"]))
        out["log"] = {"path": str(log), "size_mb": _mb(log.stat().st_size), **scan}
        bad = scan["counts"]["fatal"] + scan["counts"]["severe"]
        if bad:
            warnings.append(f"QuickBooks logged {bad} fatal or severe errors since {scan['since']}: see log.recent.")
    else:
        out["log"] = {"found": False}
    services = quickbooks_services()
    if services is not MISSING:
        out["services"] = services
    if warnings:
        out["warnings"] = warnings
    return out


# ---------------------------------------------------------------- the Web Connector

# 20261015.01:43:55 UTC	: QBWebConnector.SOAPWebService.do_authenticate() : QBWC1012: Authentication failed due to ...
WC_LINE = re.compile(r"^(\d{4})(\d\d)(\d\d)\.(\d\d:\d\d:\d\d) UTC\t: (.*?) : (.*)$")
WC_CODE = re.compile(r"\bQBWC\d{4}\b")
WC_APP = re.compile(r"updateWS\(\) for application = '?(.+?)'? has STARTED$|Performing SCHEDULED UPDATE for application = '?(.+?)'?$"
                    r"|^AppName: '?(.+?)'?$|^Application name = '?(.+?)'?$")
WC_DONE = re.compile(r"Update for application = '?(.+?)'? has COMPLETED$")
WC_NOT_VALID = re.compile(r"^(.+): Not a valid username and password\.")
WC_KEY = r"Software\Intuit\QBWebConnector"
WC_BOILERPLATE = {"--", "The request failed with the error message:"}
# What the codes mean, from Intuit's QuickBooks Web Connector Programmer's Guide (Appendix A) and its help articles
# "Fix common Web Connector errors", "Fix error QBWC1039" and "Fix QBWC1085".
NO_QUICKBOOKS = ("The Web Connector could not reach QuickBooks: QuickBooks is not running, no company file is open, or a window is "
                 "open in QuickBooks. Open QuickBooks with the company file, close open windows, and run the app again.")
SERVER_FAILED = ("The app's web server failed while the Web Connector {}. The problem is usually on the app's side: send the "
                 "error and the log (QWCLog.txt) to the app's support.")
WC_CODES = {
    "QBWC1005": "The Web Connector could not create a file it needs. Make sure your Windows user may write to C:\\ProgramData\\Intuit\\QBWebConnector.",
    "QBWC1006": "The Web Connector could not open its log, QWCLog.txt: it is missing or write-protected. Remove the write protection, then open the Web Connector again.",
    **dict.fromkeys(("QBWC1007", "QBWC1008", "QBWC1009", "QBWC1013", "QBWC1022", "QBWC1023", "QBWC1024"), NO_QUICKBOOKS),
    "QBWC1010": "The app's web address starts with http://, and the Web Connector accepts only https:// for a server on the internet. Ask the app's support for a .qwc file with an https address.",
    "QBWC1011": "The app is missing from the Web Connector's settings. Close and reopen the Web Connector; if it is still missing, add the app's .qwc file again (the app's provider has it).",
    "QBWC1012": SERVER_FAILED.format("signed in to it (authenticate)"),
    "QBWC1014": "QuickBooks did not answer the Web Connector's first questions (host, company, preferences), so the run stopped. Restart QuickBooks with the company file open and run the app again.",
    "QBWC1016": "No app was ticked: tick the app, then click Update Selected.",
    "QBWC1020": "Some apps are set to run automatically, and they cannot run while the Web Connector is closed.",
    "QBWC1030": "No password is saved for this app: type it in the Web Connector's Password column and press Enter (the app's support has it).",
    "QBWC1031": "A scheduled run ended with an error and the app was told. The log lines just before it say what failed.",
    "QBWC1032": "A scheduled run could not find the app in the Web Connector's settings. Untick Auto-Run, close and reopen the Web Connector; if that fails, add the app's .qwc file again.",
    "QBWC1033": "The Web Connector could not start its log, QWCLog.txt: the file or its folder is missing or read-only, or the disk is full. It does not run until that is fixed.",
    "QBWC1035": "The app's .qwc file is wrong or outdated: get a new one from the app's provider.",
    "QBWC1036": SERVER_FAILED.format("checked which Web Connector versions the app supports (clientVersion)"),
    "QBWC1037": "The app refused this Web Connector version, or warned about it. Follow the app's message; it usually means updating the Web Connector.",
    "QBWC1038": "The password prompt was closed or the password was wrong: get the password from the app's support and type it again.",
    "QBWC1039": ("The Web Connector could not add the app, or could not open the company file the app is tied to; the text after the "
                 "code says which. Intuit's fixes: add the app with QuickBooks open as Admin in single-user mode; keep the company "
                 "file at the name and place it had when the app was added; allow the app in Edit > Preferences > Integrated "
                 "Applications > Company Preferences."),
    "QBWC1040": "The app rejected the password: get the right one from the app's support, type it in the Web Connector and press Enter.",
    "QBWC1041": SERVER_FAILED.format("asked it for the next request (sendRequestXML)"),
    "QBWC1042": SERVER_FAILED.format("gave it QuickBooks' answer (receiveResponseXML)"),
    "QBWC1043": SERVER_FAILED.format("asked it for its last error (getLastError)"),
    "QBWC1044": SERVER_FAILED.format("ended the run (closeConnection)"),
    "QBWC1045": SERVER_FAILED.format("told it that QuickBooks could not be reached (connectionError)"),
    "QBWC1046": "The app answered the Web Connector's version check in a form it does not understand: send the log (QWCLog.txt) to the app's support.",
    "QBWC1048": "The Web Connector could not verify the app server's certificate. Remove the app, download its .qwc file again and add it; if that fails, contact the app's support.",
    "QBWC1053": "A window is open in QuickBooks (for example Preferences or an edit window), and it blocks the app. Close it and run the app again; if that fails, restart QuickBooks.",
    "QBWC1085": "The Web Connector could not write its log, QWCLog.txt, which is damaged or missing. Close QuickBooks and the Web Connector, rename QWCLog.txt (Intuit suggests QWCLogOld.txt), open the Web Connector again and restart the PC.",
    "QBWC1088": "The company file does not match the one the app was added for: download the app's .qwc file again and choose Yes to replace it.",
}
# The text after a code often says more than the code: these are checked first, in order.
WC_DETAILS = [
    (r"content type of '[^']*', but expected 'text/xml'",
     "The app's web address answered with a web page or an error instead of the Web Connector service, so the app's "
     "server or its address is the problem, not QuickBooks. The server's own error, when there is one, follows in the "
     "detail: send it to the app's support."),
    (r"Not a valid username and password|did not provide a valid username",
     "The app rejected the password saved in the Web Connector: get the right one from the app's support, type it in the "
     "Password column and press Enter."),
    (r"Garbage data", "The password was not entered right: type it again in the Password column and press Enter."),
    (r"requested read-only access|StatusCode=3263",
     "QuickBooks refused to write for this app because it has read-only access (status 3263). Adding an app writes its ID "
     "into the company file, so the add failed: ask the app's support whether it should be read-only, and if not, for a "
     ".qwc file that asks for full access."),
    (r"Unique OwnerID/FileID pair",
     "The app is already registered in this company file. Intuit's fix: remove the old registration with its CP3 tool "
     "(Remove Stamp), restart QuickBooks and the Web Connector, and add the app again."),
    (r"Unable to add FileID",
     "The Web Connector could not write the app's ID into the company file. The error just before this one in the log "
     "usually says why; Intuit says it also happens when the app was added too many times, which only Intuit support can clear."),
    (r"has not accessed this QuickBooks company data file before",
     "The app was added while QuickBooks was not open as Admin. Open QuickBooks as Admin in single-user mode, then add the "
     "app's .qwc file again in the Web Connector."),
    (r"does not have permission to access the QuickBooks company data file",
     "QuickBooks does not allow this app. As Admin in single-user mode, open Edit > Preferences > Integrated Applications "
     "> Company Preferences, allow the app, and click OK."),
    (r"BeginSession.{0,40}must include the name of the data file|company data file is not open",
     "QuickBooks was not open and the app may not start it. Open QuickBooks with the company file, or turn on automatic "
     "login for the app in Edit > Preferences > Integrated Applications; if the file was renamed or moved, put it back."),
    (r"error when parsing the provided XML",
     "The app sent QuickBooks data it cannot read, such as a character it does not accept or text that is too long: send "
     "the log to the app's support."),
    (r"modal dialog", WC_CODES["QBWC1053"]),
    (r"SSL/TLS|underlying connection was closed|trust relationship",
     "The secure connection to the app's server failed. Check the PC's date and time and its internet connection; if "
     "they are right, the server's certificate or security settings are the problem, so contact the app's support."),
]


def scan_web_connector_log(text: str) -> dict:
    """Runs and errors per application from the Web Connector's log. An entry is a line with a time (UTC) and the
    lines after it that have none: error details, XML, stack traces."""
    entries: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        m = WC_LINE.match(line)
        if m:
            entries.append((f"{m[1]}-{m[2]}-{m[3]}T{m[4]}Z", m[6].strip(), []))
        elif entries and line.strip():
            entries[-1][2].append(line.strip())
    apps: dict[str, dict] = {}
    errors: dict[tuple, dict] = {}
    app = MISSING
    scheduled = MISSING  # a scheduled update counts once, whether or not its own STARTED line follows
    for when, message, more in entries:
        if m := WC_APP.search(message):
            app = next(g for g in m.groups() if g)
            if "has STARTED" in message or "SCHEDULED UPDATE" in message:
                a = apps.setdefault(app, {"runs": 0})
                if not ("has STARTED" in message and scheduled == app):
                    a["runs"] += 1
                a["last_started"] = when
                scheduled = app if "SCHEDULED UPDATE" in message else MISSING
        elif m := WC_DONE.search(message):
            apps.setdefault(m[1], {"runs": 0})["last_completed"] = when
        code, bad = WC_CODE.search(message), WC_NOT_VALID.match(message)
        if code or bad:
            who = bad[1] if bad else app
            text_ = "Not a valid username and password." if bad else message[code.end():].lstrip(": ").strip()
            e = errors.setdefault((who, code[0] if code else "", text_), {"app": who, "code": code[0] if code else MISSING, "message": text_[:200], "times": 0})
            e["times"] += 1
            e["last"] = when
            e["detail"] = " | ".join(list(dict.fromkeys(x for x in more if x not in WC_BOILERPLATE))[:4])[:400] or MISSING
            if who is not MISSING:
                apps.setdefault(who, {"runs": 0})["last_error"] = {k: v for k, v in e.items() if k not in ("app", "times")}
        if WC_DONE.search(message) or "Update completed with" in message:
            app = scheduled = MISSING  # errors logged after an update ended belong to no app
    return {"from": entries[0][0] if entries else MISSING, "to": entries[-1][0] if entries else MISSING, "apps": apps,
            "errors": sorted(errors.values(), key=lambda e: e["last"], reverse=True)}


def web_connector_registry() -> dict | None:
    """The Web Connector's settings for this Windows user: its own values, and per application the registration
    (SOAPWebServices) and the schedule (Scheduler). None when it was never set up here."""
    if sys.platform != "win32":
        return None
    import winreg

    def read(path: str) -> tuple[dict, list[str]]:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            values, names = {}, []
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, value, _ = winreg.EnumValue(k, i)
                values[name] = value
            for i in range(winreg.QueryInfoKey(k)[0]):
                names.append(winreg.EnumKey(k, i))
            return values, names

    def apps(sub: str) -> dict[str, dict]:
        try:
            _, names = read(f"{WC_KEY}\\{sub}")
        except OSError:
            return {}
        return {n: read(f"{WC_KEY}\\{sub}\\{n}")[0] for n in names}

    try:
        settings, _ = read(WC_KEY)
    except OSError:
        return None
    try:
        version = read(f"{WC_KEY}\\Version")[0].get("ProductVersion", MISSING)
    except OSError:
        version = MISSING
    return {"settings": settings, "version": version, "apps": apps("SOAPWebServices"), "schedule": apps("Scheduler")}


def process_running(image: str) -> Any:
    if sys.platform != "win32":
        return MISSING
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"], capture_output=True, text=True,
                             errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return MISSING
    return f'"{image.lower()}"' in out.lower()


def _site(url: Any) -> Any:
    """An app's address without its query string or sign-in part, which can hold keys."""
    if not isinstance(url, str) or not url:
        return MISSING
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}{f':{u.port}' if u.port else ''}{u.path}"


def explain(code: Any, detail: str) -> Any:
    for pattern, text_ in WC_DETAILS:
        if re.search(pattern, detail, re.I):
            return text_
    return WC_CODES.get(code, MISSING)


@tool(
    "qb_web_connector",
    "Web Connector apps and their errors",
    "Read-only check of the QuickBooks Web Connector, which other apps (store sync, CRM, payroll services) use to "
    "reach QuickBooks: each app it runs, its web address, the company file it is tied to, its schedule and the last "
    "result it showed, plus each app's runs and errors from the Web Connector's log (QWCLog.txt), with what the "
    "error codes mean and what to do. Use it when a sync stopped, shows an error, or before calling the app's support.",
    {},
)
def web_connector(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    reg = web_connector_registry()
    folder = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "Intuit" / "QBWebConnector" / "log"
    logs = [p for p in (folder / "QWCLogOld.txt", folder / "QWCLog.txt") if p.is_file()]
    if reg is None and not logs:
        return {"installed": False, "note": "The Web Connector was never set up for this Windows user on this PC: run qbd as the user who runs it."}
    text_ = ""
    for p in logs:
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - LOG_TAIL))
            text_ += f.read().decode("utf-8", errors="replace") + "\n"
    scan = scan_web_connector_log(text_)
    warnings: list[str] = []
    out_apps = []

    def runs(seen: dict) -> dict:
        return {
            **({"runs_in_log": seen["runs"]} if seen.get("runs") else {}),
            **{k: seen[k] for k in ("last_started", "last_completed", "last_error") if k in seen},
            # The log's times are UTC, so an evening run west of Greenwich is dated tomorrow.
            **({"days_since_last_run": max(0, days_between(seen["last_started"][:10], today))} if "last_started" in seen else {}),
        }

    registered = (reg or {}).get("apps", {})
    for name, r in registered.items():
        when = (reg or {}).get("schedule", {}).get(name, {})
        seen = scan["apps"].get(name, {})
        llr = re.sub(r"\s+", " ", str(r.get("Llr") or "")).strip()
        code = WC_CODE.search(llr)
        status = str(r.get("MsgSeverity") or "")
        company = r.get("CompanyFile") or MISSING
        app = {
            "name": name, "url": _site(r.get("AppURL")), "company_file": company,
            "company_file_found": Path(company).is_file() if company and sys.platform == "win32" else MISSING,
            "user": r.get("UserName") or MISSING, "password_saved": str(r.get("saveThisPassword")).lower() == "true" and bool(r.get("Password")),
            "auto_run": str(when.get("Enabled")).lower() == "true",
            "every_minutes": round2(int(when["IntervalInSeconds"]) / 60) if str(when.get("IntervalInSeconds", "")).isdigit() else MISSING,
            "last_result": {k: v for k, v in {"status": status, "message": r.get("Slr") or MISSING, "code": code[0] if code else MISSING,
                                              "detail": llr[:300] or MISSING}.items() if v not in (MISSING, "")},
            **runs(seen),
        }
        if status == "Error":
            app["what_it_means"] = explain(code[0] if code else MISSING, llr)
            warnings.append(f"{name}: {r.get('Slr') or 'error'}" + (f" ({code[0]})" if code else "") + ".")
        if app["company_file_found"] is False:
            warnings.append(f"{name} is tied to {company}, which is not there any more: it cannot run until it is added again for the file in use.")
        out_apps.append({k: v for k, v in app.items() if v is not MISSING})
    out_apps += [{"name": name, "settings_found": False, **runs(seen)} for name, seen in sorted(scan["apps"].items()) if name not in registered]
    running = process_running("QBWebConnector.exe")
    if running is False and any(x.get("auto_run") for x in out_apps):
        warnings.append("Some apps are set to run automatically, but the Web Connector is not running, so they do not.")
    errors = [{**e, **({"what_it_means": m} if (m := explain(e["code"], f"{e['message']} {e['detail'] or ''}")) is not MISSING else {})}
              for e in scan["errors"][:15]]
    settings = (reg or {}).get("settings", {})
    return {
        "as_of": today,
        "installed": True,
        "version": (reg or {}).get("version", MISSING),
        "running": running,
        "apps": out_apps,
        **({"note": "The Web Connector has no settings for this Windows user, so the apps listed come from its log only: run qbd "
                    "as the user who runs the Web Connector to see their settings and last results."} if reg is None else {}),
        "log": {"files": [str(p) for p in logs], "from": scan["from"], "to": scan["to"], "log_level": settings.get("Level", MISSING), "errors": errors},
        **({"warnings": warnings} if warnings else {}),
    }


FILE_TOOLS = [file_health, web_connector]
