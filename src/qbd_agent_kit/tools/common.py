"""What every tool gets, and small helpers shared by the tools."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..config import Config
from ..jsutil import MISSING, add_up, dumps, finite, get, nz, round2, text
from ..qb.client import QbClient
from .args import Arg

TOKEN = re.compile(r"[0-9a-f]{8}\Z")


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


class Drafts:
    """Drafted changes waiting for approval. Kept in memory, or as files in a folder when every command runs in a
    process of its own. A draft is used once: taking it removes it."""

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
            old = self._load(name[:-5]) if name.endswith(".json") else None
            if old is None or old.expires < now:
                try:
                    os.remove(os.path.join(self.folder, name))
                except OSError:
                    pass
        with open(self._path(d.token), "w", encoding="utf-8") as f:
            f.write(dumps(asdict(d)))

    def _load(self, token: str) -> Draft | None:
        try:
            with open(self._path(token), encoding="utf-8") as f:
                data = json.load(f)
            return Draft(**{**data, "reason": data.get("reason", MISSING)})
        except (OSError, ValueError, TypeError):
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
