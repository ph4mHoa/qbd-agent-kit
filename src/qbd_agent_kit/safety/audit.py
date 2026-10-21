"""Every write attempt is appended to a local JSON Lines file: who, when, what, and what QuickBooks
answered. The file never leaves the PC."""
from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import socket
from typing import Any

from ..jsutil import MISSING, dumps, text
from .privacy import mask_xml


def _who_am_i() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return socket.gethostname()


def _now() -> str:
    t = dt.datetime.now(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def write_audit(path: str, entry: dict[str, Any]) -> None:
    """entry: companyFile, action, entity, summary, reason, source, before, outcome (done, failed, unknown), ids, error,
    request."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    request = entry.get("request", MISSING)
    line = {"time": _now(), "user": _who_am_i(), **entry, "request": mask_xml(request)[:4000] if isinstance(request, str) else request}
    with open(path, "a", encoding="utf-8") as f:
        f.write(dumps(line) + "\n")


def read_audit(path: str, limit: int) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = [line for line in f.read().split("\n") if line]
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"unreadable": line[:200]})
    return out


def committed(path: str, company_file: str, source: str) -> dict[str, Any] | None:
    """The newest entry that saved, or may have saved, a change from this source to this company file."""
    return next((e for e in read_audit(path, 10 ** 9) if e.get("source") == source and e.get("outcome") in ("done", "unknown")
                 and text(e.get("companyFile", "")).lower() == company_file.lower()), None)
