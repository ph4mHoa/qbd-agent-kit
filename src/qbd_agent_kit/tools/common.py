"""What every tool gets, and small helpers shared by the tools."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable

from ..config import Config
from ..jsutil import MISSING, add_up, dumps, finite, get, nz, round2, text
from ..qb.client import QbClient
from .args import Arg

TOKEN = re.compile(r"[0-9a-f]{8}\Z")


def file_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(digests: list[str]) -> str:
    """What a person saw of a draft, as one value: the SHA-256 of its file and, for an import, of each of its records'
    files ("-" for one that is gone). The Activity Monitor sends it when the person approves; qbd commit refuses a draft
    whose fingerprint changed since."""
    return hashlib.sha256(" ".join(digests).encode()).hexdigest()[:32]


def batch_tokens(request: Any) -> list[str]:
    """The records' tokens of an import's draft."""
    try:
        tokens = json.loads(request)
    except (TypeError, ValueError):
        return []
    return [t for t in tokens if isinstance(t, str) and TOKEN.match(t)] if isinstance(tokens, list) else []


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input: dict[str, Arg]
    run: Callable[[Ctx, dict[str, Any]], Any]
    writes: bool = False        # listed only when QBD_ALLOW_WRITES is on
    destructive: bool = False


def tool(name: str, title: str, description: str, input: dict[str, Arg], writes: bool = False, destructive: bool = False):
    def define(run: Callable[[Ctx, dict[str, Any]], Any]) -> Tool:
        return Tool(name, title, description, input, run, writes, destructive)
    return define


@dataclass
class Draft:
    token: str
    action: str           # add, update, delete, void
    entity: str
    request: str          # the qbXML request element, ready to send
    message: str          # e.g. InvoiceAddRq
    summary: str
    reason: Any           # text, or MISSING
    company_file: str
    expires: float        # time.time() after which it is refused
    source: Any = MISSING           # where the change comes from; saved once per company file
    allow_duplicate: bool = False   # drafted although that source, or a matching record, was already saved
    before: Any = MISSING           # update, delete, void: the record's values before the change
    # For the person who answers it in the Activity Monitor:
    created: float = 0              # time.time() when drafted
    total: Any = MISSING            # the amount it saves, when it has one
    warnings: list = field(default_factory=list)   # what the preview warned about
    checks: list = field(default_factory=list)     # the checks it passed, in plain words
    agent: str = ""                 # who asked for it, as the call log names them
    session: str = ""
    session_title: str = ""
    program: str = ""
    settings: dict = field(default_factory=dict)   # the settings it was drafted with


ANSWER_DAYS = 2   # how long the answer to a draft answered in the Activity Monitor is kept for the assistant
EXPIRED_DAYS = 7  # how long the Activity Monitor can list a draft that expired unanswered
EXPIRED_KEEP = ("token", "action", "entity", "summary", "reason", "company_file", "created", "expires", "total", "agent",
                "session", "session_title", "program")  # what it lists: no request, no values before


class Drafts:
    """Drafted changes waiting for approval. Kept in memory, or as files in a folder when every command runs in a
    process of its own. A draft is used once: taking it removes it. When a person answers a draft in the Activity
    Monitor, <token>.answer.json keeps the answer, so the assistant that asked learns it when it commits. A draft that
    expired unanswered leaves <token>.expired.json, its summary without the request, for the monitor's list."""

    def __init__(self, folder: str | None = None):
        self.folder = folder
        self._mem: dict[str, Draft] = {}

    def _path(self, token: str) -> str:
        return os.path.join(self.folder or "", f"{token}.json")

    def put(self, d: Draft) -> None:
        now = time.time()
        if self.folder is None:
            self._mem = {t: x for t, x in self._mem.items() if x.expires >= now}
            self._mem[d.token] = d
            return
        os.makedirs(self.folder, exist_ok=True)
        for name in os.listdir(self.folder):
            path = os.path.join(self.folder, name)
            try:
                if name.endswith(".answer.json"):
                    stale = os.path.getmtime(path) < now - ANSWER_DAYS * 86400
                elif name.endswith(".expired.json"):
                    stale = os.path.getmtime(path) < now - EXPIRED_DAYS * 86400
                else:
                    old = self._load(name[:-5]) if name.endswith(".json") else None
                    if old is not None and old.expires < now:
                        self.retire(old)
                    stale = old is None
                if stale:
                    os.remove(path)
            except OSError:
                pass
        with open(self._path(d.token), "w", encoding="utf-8") as f:
            f.write(dumps(asdict(d)))

    def _load(self, token: str) -> Draft | None:
        if not TOKEN.match(token):
            return None
        try:
            with open(self._path(token), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("token") != token:
                return None  # qbd files a draft under its own token only
            known = {f.name for f in fields(Draft)}  # a newer qbd may have written more
            return Draft(**{**{k: v for k, v in data.items() if k in known}, "reason": data.get("reason", MISSING)})
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _raw(self, token: str) -> bytes | None:
        if self.folder is None or not TOKEN.match(token):
            return None
        try:
            with open(self._path(token), "rb") as f:
                return f.read()
        except OSError:
            return None

    def fingerprint(self, token: str) -> str | None:
        """The fingerprint of a draft on file, with its records for an import; None when it is not on file."""
        raw = self._raw(token)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            children = batch_tokens(data.get("request")) if data.get("action") == "batch" else []
        except (ValueError, AttributeError):
            children = []
        return fingerprint([file_digest(raw), *(file_digest(c) if (c := self._raw(t)) is not None else "-" for t in children)])

    def retire(self, d: Draft) -> None:
        """An expired draft: its file goes, and what the Activity Monitor lists of it stays a few days."""
        if self.folder is None or not TOKEN.match(d.token):
            return
        keep = {k: v for k, v in asdict(d).items() if k in EXPIRED_KEEP}
        with open(os.path.join(self.folder, f"{d.token}.expired.json"), "w", encoding="utf-8") as f:
            f.write(dumps(keep))
        gone = [f"{d.token}.json"]
        if d.action == "batch":  # an import is listed as one: its records' drafts go with it
            gone += [f"{t}{end}" for t in batch_tokens(d.request) for end in (".json", ".expired.json")]
        for name in gone:
            try:
                os.remove(os.path.join(self.folder, name))
            except OSError:
                pass

    def answer(self, token: str, answer: dict[str, Any]) -> None:
        """Keeps how a person answered a draft in the Activity Monitor: approved with what was saved, or rejected."""
        if self.folder is None or not TOKEN.match(token):
            return
        with open(os.path.join(self.folder, f"{token}.answer.json"), "w", encoding="utf-8") as f:
            f.write(dumps({"token": token, **answer}))

    def answered(self, token: str) -> dict[str, Any] | None:
        if self.folder is None or not TOKEN.match(token):
            return None
        try:
            with open(os.path.join(self.folder, f"{token}.answer.json"), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def take(self, token: str) -> Draft | None:
        if self.folder is None:
            return self._mem.pop(token, None)
        if not TOKEN.match(token):
            return None
        d = self._load(token)
        try:
            os.remove(self._path(token))
        except OSError:
            return None
        return d


@dataclass
class Ctx:
    qb: QbClient
    config: Config
    today: Callable[[], str]
    drafts: Drafts
    cache: dict[str, Any] = field(default_factory=dict)
    live: Any = None  # the command's live file for the Activity Monitor (safety.live.Live), when it has one


def date(what: str) -> Arg:
    return Arg("date", what)


def num(v: Any) -> float:
    return v if finite(v) else 0


def sum_of(xs: Any, f: Callable[[Any], float]) -> float:
    return round2(add_up(f(x) for x in xs))


@dataclass
class CompanyFacts:
    """Company settings the tools need, read once per company file."""
    name: str
    closing_date: str | None
    first_fiscal_month: int
    uses_inventory: bool
    uses_classes: bool
    uses_sites: bool  # inventory sites (Advanced Inventory)


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def company_facts(ctx: Ctx) -> CompanyFacts:
    host = ctx.qb.host_info()
    key = f"facts:{host.company_file}"
    if key in ctx.cache:
        return ctx.cache[key]
    company = ctx.qb.request("CompanyQueryRq", {"IncludeRetElement": ["CompanyName", "FirstMonthFiscalYear"]})
    prefs = ctx.qb.request("PreferencesQueryRq", {"IncludeRetElement": ["AccountingPreferences", "PurchasesAndVendorsPreferences", "MultiLocationInventoryPreferences"]})
    c = nz(company.body.get("CompanyRet", MISSING), {})
    p = nz(prefs.body.get("PreferencesRet", MISSING), {})
    acct = nz(get(p, "AccountingPreferences"), {})
    purch = nz(get(p, "PurchasesAndVendorsPreferences"), {})
    month = text(get(c, "FirstMonthFiscalYear"))
    closing = get(acct, "ClosingDate")
    facts = CompanyFacts(
        name=text(nz(get(c, "CompanyName"), "")),
        closing_date=None if closing is MISSING else closing,
        first_fiscal_month=max(1, (MONTHS.index(month) if month in MONTHS else -1) + 1),
        uses_inventory=get(purch, "IsUsingInventory") is not False,
        uses_classes=get(acct, "IsUsingClassTracking") is True,
        uses_sites=get(get(p, "MultiLocationInventoryPreferences"), "IsMultiLocationInventoryEnabled") is True,
    )
    ctx.cache[key] = facts
    return facts
