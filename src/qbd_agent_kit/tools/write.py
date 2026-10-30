"""Changes happen in two steps. qbd draft checks a change and returns a plain-language preview and a one-time token;
qbd commit sends it only when called with that token, which the agent should do only after the person approves.
Both are listed only when QBD_ALLOW_WRITES is on."""
from __future__ import annotations

import csv
import json
import math
import re
import secrets
import time
from pathlib import Path
from typing import Any

from ..errors import InvalidArguments, QbConnectionError, QbStatusError, QbxmlInputError, ToolError, WriteUnconfirmed
from ..jsutil import MISSING, add_up, dumps, get, is_num, money as fmt, nz, round2, text, to_number, truthy
from ..qb.catalog import Entity, amount_of, entities, entity, ref_name
from ..qb.dates import add_days, days_between
from ..qbxml.build import LIST_ID, build_request
from ..qbxml.parse import parse_responses
from ..qbxml.schema import Model, enums, has_message, is_model, message_model, model
from ..config import Config
from ..safety.audit import _now, committed, read_audit, who_am_i, write_audit
from ..safety.privacy import mask_personal_data
from .args import Arg, parse_args
from .common import Ctx, Draft, Drafts, company_facts, tool

WRITABLE = tuple(e.key for e in entities() if has_message(f"{e.base}AddRq") or has_message(f"{e.base}ModRq"))
TOKEN_MINUTES = 15
NOT_KEPT = "(personal data, not kept)"


def asked_by(c: Config) -> dict[str, Any]:
    """Who drafts a change and with which settings, kept with the draft for the Activity Monitor."""
    return {"created": time.time(), "agent": c.agent, "session": c.session, "session_title": c.session_title,
            "program": c.program, "settings": c.settings()}


def _arr(v: Any) -> list:
    return [] if v is None or v is MISSING else v if isinstance(v, list) else [v]


def n(v: Any) -> float:
    if is_num(v):
        return v
    x = to_number(v) if isinstance(v, str) else math.nan
    return x if math.isfinite(x) else 0


def _max(*xs: float) -> float:
    return math.nan if any(x != x for x in xs) else max(xs)


def draft_total(fields: dict) -> float:
    """The total a drafted transaction will have."""
    for k in ("TotalAmount", "Amount"):
        if k in fields:
            return round2(n(fields[k]))
    lines = debit = credit = 0
    for k, v in fields.items():
        if k == "JournalDebitLine":
            debit += add_up(n(get(l, "Amount")) for l in _arr(v))
        elif k == "JournalCreditLine":
            credit += add_up(n(get(l, "Amount")) for l in _arr(v))
        elif re.search(r"Line(Add|Mod)\Z", k):
            lines += add_up(
                n(get(l, "Amount")) if get(l, "Amount") is not MISSING else n(nz(get(l, "Quantity"), 1)) * n(nz(get(l, "Rate"), get(l, "Cost"), 0))
                for l in _arr(v)
            )
    return round2(_max(lines, debit, credit))


# Which list a reference points into, so names can be checked before QuickBooks sees them.
REF_LISTS = {
    "CustomerRef": "CustomerQueryRq",
    "VendorRef": "VendorQueryRq",
    "PrefVendorRef": "VendorQueryRq",
    "ItemRef": "ItemQueryRq",
    "AccountRef": "AccountQueryRq",
    "ARAccountRef": "AccountQueryRq",
    "APAccountRef": "AccountQueryRq",
    "BankAccountRef": "AccountQueryRq",
    "DepositToAccountRef": "AccountQueryRq",
    "IncomeAccountRef": "AccountQueryRq",
    "ExpenseAccountRef": "AccountQueryRq",
    "COGSAccountRef": "AccountQueryRq",
    "AssetAccountRef": "AccountQueryRq",
    "CreditCardAccountRef": "AccountQueryRq",
    "ClassRef": "ClassQueryRq",
    "TermsRef": "TermsQueryRq",
    "PaymentMethodRef": "PaymentMethodQueryRq",
    "ShipMethodRef": "ShipMethodQueryRq",
    "SalesTaxCodeRef": "SalesTaxCodeQueryRq",
    "CustomerSalesTaxCodeRef": "SalesTaxCodeQueryRq",
}


def _rows(ctx: Ctx, message: str, body: dict, limit: int) -> list[dict]:
    """A query whose failure counts as no match."""
    try:
        return ctx.qb.query(message, body, limit).rows
    except Exception:
        return []


def names_in(fields: dict) -> list[tuple[str, str]]:
    """The names a change refers to, each with the list it must be in: [("CustomerQueryRq", "Acme Ltd"), ...]."""
    wanted: dict[str, tuple[str, str]] = {}

    def walk(v: Any) -> None:
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, list):
                for val in item:
                    if isinstance(val, (dict, list)):
                        walk(val)
            elif isinstance(item, dict):
                for k, val in item.items():
                    lst = REF_LISTS.get(k)
                    name = val if isinstance(val, str) else nz(get(val, "FullName"), get(val, "ListID"))
                    if lst and isinstance(name, str):
                        wanted[f"{lst}|{name.lower()}"] = (lst, name)
                    elif isinstance(val, (dict, list)):
                        walk(val)

    walk(fields)
    return list(wanted.values())


NAME_KINDS = {"CustomerQueryRq": "customer", "VendorQueryRq": "vendor", "ItemQueryRq": "item", "AccountQueryRq": "account",
              "ClassQueryRq": "class", "TermsQueryRq": "terms", "PaymentMethodQueryRq": "payment method",
              "ShipMethodQueryRq": "shipping method", "SalesTaxCodeQueryRq": "tax code"}


def names_exist(fields: dict) -> list[str]:
    """The check that the names a change uses exist, in plain words, when it uses any."""
    kinds = list(dict.fromkeys(NAME_KINDS.get(lst, "other") for lst, _ in names_in(fields)))
    if not kinds:
        return []
    return [f"The {' and '.join([', '.join(kinds[:-1]), kinds[-1]] if len(kinds) > 1 else kinds)} name{'s' if len(names_in(fields)) > 1 else ''} it uses exist"]


def check_refs(ctx: Ctx, fields: dict) -> list[str]:
    problems = []
    seen = ctx.cache.get("refs")  # names already found during one qbd import
    for lst, name in names_in(fields):
        if seen is not None and f"{lst}|{name.lower()}" in seen:
            continue
        if _rows(ctx, lst, {"ListID": name} if LIST_ID.match(name) else {"FullName": name}, 1):
            if seen is not None:
                seen.add(f"{lst}|{name.lower()}")
            continue
        what = lst.replace("QueryRq", "").lower()
        word = next((w for w in re.split(r"[\s:]+", name) if len(w) >= 3), name)
        like = _rows(ctx, lst, {"ActiveStatus": "All", "NameFilter": {"MatchCriterion": "Contains", "Name": word}}, 5)
        names = [text(nz(get(r, "FullName"), get(r, "Name"))) for r in like]
        problems.append(f'No {what} named "{name}"' + (". Did you mean: " + ", ".join(f'"{x}"' for x in names) + "?" if names else "."))
    return problems


def find_existing(ctx: Ctx, e: Entity, id_: Any = MISSING, name: Any = MISSING) -> dict | None:
    body: dict[str, Any] = {}
    if truthy(id_):
        body["ListID" if e.kind == "list" else "TxnID"] = id_
    elif truthy(name) and e.kind == "list":
        body["FullName"] = name
    else:
        return None
    if e.kind == "txn":
        body["IncludeLineItems"] = True
    res = ctx.qb.query(e.query, body, 2)
    return res.rows[0] if res.rows else None


def line_keys(record: dict) -> list[str]:
    return [k for k in record if re.search(r"Line(Group)?Ret\Z", k)]


def describe(e: Entity, rec: dict) -> str:
    who = ref_name(nz(get(rec, "CustomerRef"), get(rec, "VendorRef"), get(rec, "PayeeEntityRef"), get(rec, "EntityRef")))
    if e.kind == "list":
        return f'{noun(e)} "{text(nz(get(rec, "FullName"), get(rec, "Name"), get(rec, "ListID")))}"'
    amount = amount_of(rec)
    parts = [
        noun(e) + (f" #{text(rec['RefNumber'])}" if truthy(get(rec, "RefNumber")) else ""),
        f"for {text(who)}" if truthy(who) else "",
        f"dated {text(rec['TxnDate'])}" if truthy(get(rec, "TxnDate")) else "",
        f"({fmt(amount)})" if amount is not MISSING else "",
    ]
    return " ".join(p for p in parts if p)


def _ref(v: Any) -> Any:
    return ref_name({"FullName": v} if isinstance(v, str) else v)


def line_text(l: Any) -> str:
    what = nz(_ref(get(l, "ItemRef")), _ref(get(l, "AccountRef")), text(nz(get(l, "Desc"), "line")))
    rate = n(nz(get(l, "Rate"), get(l, "Cost")))
    if get(l, "Quantity") is not MISSING:
        amount = n(l["Amount"]) if get(l, "Amount") is not MISSING else n(l["Quantity"]) * rate
        return f"{text(l['Quantity'])} x {text(what)} @ {fmt(rate)} = {fmt(amount)}"
    return f"{text(what)}: {fmt(n(get(l, 'Amount')))}"


NOUNS = {
    "receive_payment": "customer payment",
    "bill_payment_check": "bill payment by check",
    "bill_payment_credit_card": "bill payment by card",
    "sales_tax_payment": "sales tax payment",
}


def noun(e: Entity) -> str:
    return NOUNS.get(e.key, e.key.replace("_", " "))


def shown(v: Any) -> str:
    if v is None or v is MISSING:
        return "(empty)"
    if isinstance(v, (dict, list)):
        return text(nz(ref_name(v), dumps(v)))
    return text(v)


def safe(k: str, v: Any) -> str:
    """A field's value for the preview and the write log, with card, ID and payroll data masked as in every answer."""
    m = mask_personal_data({k: v})
    return shown(m[k]) if k in m else "(personal data)"


# ---------------------------------------------------------------- what QuickBooks fills in

SELLING = ("invoice", "estimate", "sales_order", "sales_receipt", "credit_memo")
TXN_KEYS = ("TxnID", "LinkToTxnID", "PaymentTxnID")  # fields that point at another transaction
OPEN_DONE = {"SalesOrder": "Invoiced", "PurchaseOrder": "ReceivedQuantity"}  # line quantity already turned into the next document


def item_price(item: Any, buying: bool) -> Any:
    both, either = get(item, "SalesAndPurchase"), get(item, "SalesOrPurchase")
    if buying:
        return nz(get(item, "PurchaseCost"), get(both, "PurchaseCost"), get(either, "Price"), MISSING)
    return nz(get(item, "SalesPrice"), get(both, "SalesPrice"), get(either, "Price"), MISSING)


def lookups(ctx: Ctx, fields: dict) -> tuple[dict, dict]:
    """The items (by lower-case full name) and transactions (by TxnID, with "open" for invoices and orders) a draft
    names, each list read once."""
    items: set[str] = set()
    ids: set[str] = set()

    def walk(v: Any, key: str) -> None:
        if key == "ItemRef":
            items.add(text(_ref(v)))
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif key in TXN_KEYS and isinstance(v, str):
            ids.add(v)

    walk(fields, "")
    found = {text(get(r, "FullName")).lower(): r for r in _rows(ctx, "ItemQueryRq", {"FullName": sorted(items)}, len(items) + 1)} if items else {}
    txns = {text(get(r, "TxnID")): dict(r) for r in _rows(ctx, "TransactionQueryRq", {"TxnID": sorted(ids)}, len(ids) + 1)} if ids else {}
    for kind in ("Invoice", *OPEN_DONE):
        mine = sorted(i for i, t in txns.items() if get(t, "TxnType") == kind)
        if not mine:
            continue
        body = {"TxnID": mine, "IncludeLineItems": True} if kind in OPEN_DONE else {"TxnID": mine, "IncludeRetElement": ["TxnID", "BalanceRemaining"]}
        for r in _rows(ctx, f"{kind}QueryRq", body, len(mine) + 1):
            if kind == "Invoice":
                left = n(get(r, "BalanceRemaining"))
            else:
                lines = _arr(get(r, f"{kind}LineRet")) + [l for g in _arr(get(r, f"{kind}LineGroupRet")) for l in _arr(get(g, f"{kind}LineRet"))]
                left = add_up(max(0, n(get(l, "Quantity")) - n(get(l, OPEN_DONE[kind]))) * n(get(l, "Rate")) if get(l, "Quantity") is not MISSING else 0
                              for l in lines if not truthy(get(l, "IsManuallyClosed")))
            txns[text(get(r, "TxnID"))]["open"] = round2(left)
    return found, txns


def link_text(t: Any, id_: str) -> str:
    if t is None:
        return f"transaction {id_} (not found)"
    kind = next((noun(x) for x in entities() if x.base == get(t, "TxnType")), re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text(get(t, "TxnType"))).lower())
    ref = f" {text(t['RefNumber'])}" if truthy(get(t, "RefNumber")) else ""
    open_ = f", open {fmt(t['open'])}" if "open" in t else ""
    return f"{kind}{ref} ({text(ref_name(get(t, 'EntityRef')))}, {text(get(t, 'TxnDate'))}, {fmt(n(get(t, 'Amount')))}{open_})"


def txn_preview(ctx: Ctx, e: Entity, fields: dict) -> tuple[list[str], float, list[str], list[str]]:
    """A drafted transaction's lines as QuickBooks will save them, its total, what in that total is estimated, and
    warnings. QuickBooks fills in what a draft leaves out (item prices, a linked order's open lines, a deposit's
    payments), so the preview reads those first: the person approves what will be saved, and the amount limit applies
    to it."""
    items, txns = lookups(ctx, fields)
    buying = e.key not in SELLING
    out: list[str] = []
    warnings: list[str] = []
    money: list[float] = []
    debit = credit = 0.0
    about: list[str] = []
    for k, v in fields.items():
        if not re.search(r"Line(Add)?\Z", k):
            continue
        for l in _arr(v):
            item = items.get(text(_ref(get(l, "ItemRef"))).lower()) if truthy(get(l, "ItemRef")) else None
            what = text(nz(_ref(get(l, "ItemRef")), _ref(get(l, "AccountRef")), _ref(get(l, "EntityRef")), "line"))
            if k in ("JournalDebitLine", "JournalCreditLine"):
                amount = n(get(l, "Amount"))
                debit, credit = (debit + amount, credit) if k == "JournalDebitLine" else (debit, credit + amount)
                who = f" ({text(_ref(l['EntityRef']))})" if truthy(get(l, "EntityRef")) else ""
                out.append(f"{'Debit' if k == 'JournalDebitLine' else 'Credit'} {what} {fmt(amount)}{who}")
            elif truthy(get(l, "PaymentTxnID")):
                t = txns.get(text(l["PaymentTxnID"]))
                if t is None:
                    warnings.append(f"Payment {text(l['PaymentTxnID'])} was not found; QuickBooks will refuse this deposit.")
                money.append(n(get(t, "Amount")))
                out.append(f"Deposit {link_text(t, text(l['PaymentTxnID']))}")
            elif truthy(get(l, "QuantityToTransfer")):
                out.append(f"{text(l['QuantityToTransfer'])} x {what}")
            elif k == "InventoryAdjustmentLineAdd":
                adj = nz(get(l, "QuantityAdjustment"), get(l, "ValueAdjustment"), {})
                have = get(item, "QuantityOnHand") if not truthy(get(fields, "InventorySiteRef")) else MISSING
                new = nz(get(adj, "NewQuantity"), round2(n(have) + n(get(adj, "QuantityDifference"))) if is_num(have) and get(adj, "QuantityDifference") is not MISSING else MISSING, MISSING)
                change = n(new) - n(have) if is_num(have) and new is not MISSING else n(get(adj, "QuantityDifference"))
                parts = [f"quantity {text(have)} -> {text(new)}" if is_num(have) and new is not MISSING else
                         f"new quantity {text(new)}" if new is not MISSING else f"quantity {change:+g}" if change else ""]
                for key, label in (("NewValue", "value ->"), ("ValueDifference", "value change")):
                    if get(adj, key) is not MISSING:
                        parts.append(f"{label} {fmt(n(adj[key]))}")
                value = n(get(adj, "ValueDifference")) if get(adj, "ValueDifference") is not MISSING else change * n(get(item, "AverageCost"))
                money.append(abs(value))
                if get(adj, "ValueDifference") is MISSING and value:
                    parts.append(f"value about {'+' if value > 0 else ''}{fmt(value)} at average cost")
                out.append(f"{what}: {', '.join(p for p in parts if p) or 'no change given'}")
            else:
                rate = nz(get(l, "Rate"), get(l, "Cost"), MISSING)
                filled = ""
                if rate is MISSING and get(l, "Amount") is MISSING and item is not None and truthy(get(l, "ItemRef")):
                    rate = item_price(item, buying)
                    filled = f" (item {'cost' if buying else 'price'})" if rate is not MISSING else ""
                    if rate is not MISSING:
                        about.append(f"the item {'costs' if buying else 'prices'}")
                    if rate is MISSING:
                        warnings.append(f"{what} has no rate here and no {'cost' if buying else 'price'} in the item list; the total leaves it out.")
                qty = get(l, "Quantity")
                amount = n(l["Amount"]) if get(l, "Amount") is not MISSING else n(nz(qty, 1)) * n(rate)
                money.append(amount)
                link = get(l, "LinkToTxn")
                came = f" (from {link_text(txns.get(text(get(link, 'TxnID'))), text(get(link, 'TxnID')))})" if truthy(link) else ""
                out.append(f"{text(qty)} x {what} @ {fmt(n(rate))}{filled} = {fmt(amount)}{came}" if qty is not MISSING
                           else f"{what}: {fmt(amount)}{filled}{came}")
    for id_ in _arr(get(fields, "LinkToTxnID")):
        t = txns.get(text(id_))
        out.append(f"Brings in the open lines of {link_text(t, text(id_))}")
        money.append(n(get(t, "open")) if t is not None and "open" in t else n(get(t, "Amount")))
        about.append("the linked lines")
    applied = 0.0
    for x in _arr(get(fields, "AppliedToTxnAdd")):
        t = txns.get(text(get(x, "TxnID")))
        pay = n(get(x, "PaymentAmount"))
        applied += pay
        out.append(f"Applies {fmt(pay)} to {link_text(t, text(get(x, 'TxnID')))}")
        if t is not None and "open" in t and pay > t["open"]:
            warnings.append(f"{fmt(pay)} is more than the {fmt(t['open'])} still open on {text(get(t, 'RefNumber'))}.")
    cash_back = n(get(get(fields, "CashBackInfoAdd"), "Amount"))
    if cash_back:
        out.append(f"Cash back {fmt(cash_back)} to {text(_ref(get(fields['CashBackInfoAdd'], 'AccountRef')))}")
    if debit or credit:
        if round2(debit) != round2(credit):
            warnings.append(f"Debits {fmt(debit)} and credits {fmt(credit)} differ; QuickBooks refuses an unbalanced journal entry.")
        total = round2(max(debit, credit))
    elif get(fields, "TotalAmount") is not MISSING or get(fields, "Amount") is not MISSING:
        total = draft_total(fields)
        if applied and round2(applied) < total and e.key == "receive_payment":
            out.append(f"Left over: {fmt(total - applied)} stays as a credit for the customer")
        elif round2(applied) > total:
            warnings.append(f"The amounts applied ({fmt(applied)}) are more than the payment ({fmt(total)}).")
    else:
        total = round2(add_up(money) - cash_back)
    return out, total, list(dict.fromkeys(about)), warnings


@tool(
    "qb_draft",
    "Draft a change to QuickBooks",
    "Prepare an add, update, upsert, delete or void, and check it without saving anything: names must exist, the date must "
    "be after the closing date, the amount under the limit, and it must not duplicate an existing record. Returns a "
    "preview and a token. Show the preview to the person and run qbd commit with the token only after they say yes. "
    "Field names are qbXML names (see qbd schema, e.g. InvoiceAdd); references can be plain full names.",
    {
        "action": Arg("enum", "upsert = update the list record with this name, or add it", required=True, choices=("add", "update", "upsert", "delete", "void")),
        "type": Arg("enum", "Record type, e.g. customer, vendor, invoice, bill, receive_payment, purchase_order", required=True, choices=WRITABLE),
        "fields": Arg(
            "dict",
            'For add/update: the fields, e.g. {"CustomerRef": "Acme Ltd", "TxnDate": "2026-10-20", "InvoiceLineAdd": [{"ItemRef": "Widget", "Quantity": 2, "Rate": 10}]}',
            default={},
        ),
        "id": Arg("str", "ListID or TxnID of the record to update, delete or void"),
        "name": Arg("str", "Lists: full name of the record to update, upsert or delete"),
        "reason": Arg("str", "Why this change is made; kept in the audit log"),
        "source": Arg("str", "Where the change comes from (a file name, an email's Message-ID, a CSV row); a source already "
                             "saved to this company file is refused, so the same document is never entered twice"),
        "allow_duplicate": Arg("bool", "Draft it even though a matching record exists, or its source was already saved", default=False),
        "remove_lines": Arg("list", "Update: TxnLineIDs that the change removes on purpose"),
    },
    writes=True,
)
def draft(ctx: Ctx, a: dict) -> dict:
    c = ctx.config
    e = entity(a["type"])
    if e.key not in c.write_entities:
        raise ToolError(f"Changing {e.key} records is not allowed here. Allowed: {', '.join(c.write_entities)} (QBD_WRITE_ENTITIES).")
    if a["action"] in ("delete", "void") and not c.allow_delete:
        raise ToolError("Deleting and voiding are turned off (QBD_ALLOW_DELETE).")
    facts = company_facts(ctx)
    host = ctx.qb.host_info()
    today = ctx.today()
    fields = dict(a["fields"])
    warnings: list[str] = []
    checks: list[str] = []  # what it passed, for the person who approves it in the Activity Monitor
    source = a["source"].strip() if truthy(a["source"]) else MISSING
    if source is not MISSING and not a["allow_duplicate"]:
        refuse_saved_source(ctx, host.company_file, source)
        checks.append("Not saved before from the same source")
    before = MISSING
    total: Any = MISSING

    def closed(d: Any, what: str) -> None:
        if facts.closing_date and isinstance(d, str) and d <= facts.closing_date:
            raise ToolError(f"{what} is dated {d}, on or before the closing date {facts.closing_date}. Closed periods are not changed from here.")
        if facts.closing_date and isinstance(d, str):
            checks.append(f"Dated after the closing date, {facts.closing_date}")

    action = a["action"]
    existing = None if action == "add" else find_existing(ctx, e, a["id"], nz(a["name"], get(fields, "Name") if action == "upsert" else MISSING))
    if action == "upsert":
        if e.kind != "list":
            raise ToolError("upsert works for list records such as customers and vendors; use add or update for transactions.")
        action = "update" if existing is not None else "add"
        if existing is None and not truthy(get(fields, "Name")) and truthy(a["name"]):
            fields["Name"] = a["name"]
    elif action != "add" and existing is None:
        which = f" with id {a['id']}" if truthy(a["id"]) else f' named "{a["name"]}"' if truthy(a["name"]) else ""
        raise ToolError(f"No {e.key} found{which}. Give id{' or name' if e.kind == 'list' else ''} from a search.")

    token = secrets.token_hex(4)
    if action == "add":
        message = f"{e.base}AddRq"
        if not has_message(message):
            raise ToolError(f"QuickBooks cannot add {e.key} records through the SDK.")
        if e.base == "Check" and truthy(get(fields, "RefNumber")) and "IsToBePrinted" not in fields:
            fields["IsToBePrinted"] = False  # else QuickBooks can mark it to be printed and drop the number
        request = build_request(message, {f"{e.base}Add": fields}, {"requestID": token})
        problems = check_refs(ctx, fields)
        if problems:
            raise ToolError("Fix these before drafting:\n- " + "\n- ".join(problems))
        checks += names_exist(fields)
        if e.kind == "list":
            name = text(nz(get(fields, "Name"), ""))
            same = find_existing(ctx, e, MISSING, name)
            if same is not None and not a["allow_duplicate"]:
                raise ToolError(f'A {e.key} named "{name}" already exists (ListID {text(get(same, "ListID"))}). Use action update or upsert.')
            if same is None:
                checks.append(f'No {noun(e)} named "{name}" yet')
            summary = [f'Add {noun(e)} "{name}"', *(f"- {k}: {safe(k, v)}" for k, v in fields.items() if k != "Name")]
        else:
            lines, total, about, more = txn_preview(ctx, e, fields)
            warnings += more
            if total > c.max_amount:
                raise ToolError(f"The total {fmt(total)} is above the limit of {fmt(c.max_amount)} (QBD_MAX_AMOUNT).")
            checks.append(f"Under the limit for changes, {fmt(c.max_amount)}")
            txn_date = nz(get(fields, "TxnDate"), today)
            closed(txn_date, "This transaction")
            if days_between(today, txn_date) > 31:
                warnings.append(f"The date {text(txn_date)} is more than a month ahead.")
            party_key = next((k for k in ("CustomerRef", "VendorRef", "PayeeEntityRef") if k in fields), None)
            party = _ref(fields[party_key]) if party_key else MISSING
            if truthy(party) and "TxnDate" in e.fields:
                around = _rows(ctx, e.query, {
                    "TxnDateRangeFilter": {"FromTxnDate": add_days(txn_date, -30), "ToTxnDate": add_days(txn_date, 30)},
                    "EntityFilter": {"FullNameWithChildren": party},
                }, 200)
                ref = get(fields, "RefNumber")
                same_ref = next((r for r in around if text(nz(get(r, "RefNumber"), "")).lower() == text(ref).lower()), None) if truthy(ref) else None
                if same_ref is not None and not a["allow_duplicate"]:
                    raise ToolError(
                        f"{text(party)} already has a {e.key} #{text(ref)} (TxnID {text(get(same_ref, 'TxnID'))}, dated {text(get(same_ref, 'TxnDate'))}). "
                        "Set allow_duplicate if this is really a second one."
                    )
                if truthy(ref) and same_ref is None:
                    checks.append(f"Number {text(ref)} not used for {text(party)} within 30 days of its date")
                same_amount = [r for r in around if amount_of(r) == total and abs(days_between(text(get(r, "TxnDate")), txn_date)) <= 3]
                if same_amount:
                    warnings.append(f"Possible duplicate: {'; '.join(describe(e, r) for r in same_amount)}.")
                else:
                    checks.append(f"No other {noun(e)} for {text(party)} with the same amount within 3 days")
            skip = ("TxnDate", "RefNumber", "TotalAmount", "Amount", party_key, "LinkToTxnID", "AppliedToTxnAdd", "CashBackInfoAdd")
            for_party = f" for {text(party)}" if truthy(party) else ""
            number = f", number {text(fields['RefNumber'])}" if truthy(get(fields, "RefNumber")) else ""
            worth = "" if e.key in ("transfer_inventory", "inventory_adjustment") else "total"
            summary = [
                f"Add {noun(e)}{for_party} dated {text(txn_date)}{number}" + (f", {worth} {'about ' if about else ''}{fmt(total)}" if worth else ""),
                *(f"- {x}" for x in lines[:15]),
                *([f"- and {len(lines) - 15} more line(s)"] if len(lines) > 15 else []),
                *(f"- {k}: {safe(k, v)}" for k, v in fields.items() if not re.search(r"Line(Add)?\Z", k) and k not in skip),
                *([f"(about: QuickBooks fills in {' and '.join(about)} when it saves"
                     + ("; a price level on the customer can change the prices)" if "the item prices" in about else ")")] if about else []),
            ]
    elif action == "update":
        message = f"{e.base}ModRq"
        if not has_message(message):
            raise ToolError(f"QuickBooks cannot change {e.key} records through the SDK.")
        rec = existing
        id_field = "ListID" if e.kind == "list" else "TxnID"
        closed(get(rec, "TxnDate"), f"This {e.key}")
        closed(get(fields, "TxnDate"), "The new date")
        # In qbXML, lines left out of a change are deleted. Make that explicit.
        line_mods = [k for k in fields if re.search(r"LineMod\Z", k)]
        if line_mods:
            kept = {text(get(l, "TxnLineID")) for k in line_mods for l in _arr(fields[k])}
            old = [text(get(l, "TxnLineID")) for k in line_keys(rec) for l in _arr(rec[k])]
            dropped = [i for i in old if i not in kept and i not in nz(a["remove_lines"], [])]
            if dropped:
                raise ToolError(f"QuickBooks deletes every line left out of a change. Include lines {', '.join(dropped)} (with their TxnLineID) or list them in remove_lines.")
            total = draft_total(fields)
            if total > c.max_amount:
                raise ToolError(f"The new total {fmt(total)} is above the limit of {fmt(c.max_amount)} (QBD_MAX_AMOUNT).")
            checks.append(f"Under the limit for changes, {fmt(c.max_amount)}")
        problems = check_refs(ctx, fields)
        if problems:
            raise ToolError("Fix these before drafting:\n- " + "\n- ".join(problems))
        checks += names_exist(fields)
        request = build_request(message, {f"{e.base}Mod": {id_field: get(rec, id_field), "EditSequence": get(rec, "EditSequence"), **fields}}, {"requestID": token})
        before = {**{k: rec.get(k) for k in fields if not re.search(r"LineMod\Z", k)}, **({k: rec[k] for k in line_keys(rec)} if line_mods else {})}

        def line_id(l: Any) -> str:
            i = text(get(l, "TxnLineID"))
            return "(new)" if i == "-1" else i

        summary = [
            f"Change {describe(e, rec)}",
            *(f"- {k}: {safe(k, get(rec, k))} -> {safe(k, v)}" for k, v in fields.items() if not re.search(r"LineMod\Z", k)),
            *(f"- line {line_id(l)}: {line_text(l)}" for k in line_mods for l in _arr(fields[k])),
            *([f"- remove line(s) {', '.join(a['remove_lines'])}"] if a["remove_lines"] else []),
        ]
    else:
        rec = existing
        closed(get(rec, "TxnDate"), f"This {e.key}")
        if action == "void":
            if e.kind != "txn" or e.base not in enums()["TxnVoidType"]:
                raise ToolError(f"{e.key} records cannot be voided; delete instead.")
            message = "TxnVoidRq"
            request = build_request(message, {"TxnVoidType": e.base, "TxnID": get(rec, "TxnID")}, {"requestID": token})
        elif e.kind == "txn":
            if e.base not in enums()["TxnDelType"]:
                raise ToolError(f"{e.key} records cannot be deleted through the SDK.")
            message = "TxnDelRq"
            request = build_request(message, {"TxnDelType": e.base, "TxnID": get(rec, "TxnID")}, {"requestID": token})
        else:
            if e.base not in enums()["ListDelType"]:
                raise ToolError(f"{e.key} records cannot be deleted through the SDK; make them inactive with an update instead.")
            message = "ListDelRq"
            request = build_request(message, {"ListDelType": e.base, "ListID": get(rec, "ListID")}, {"requestID": token})
            balance = get(rec, "Balance")
            if is_num(balance) and balance != 0:
                warnings.append(f"It has a balance of {fmt(balance)}; QuickBooks refuses to delete names that are used in transactions.")
        summary = [f"{'Void' if action == 'void' else 'Delete'} {describe(e, rec)}"]
        before = rec
        total = amount_of(rec) if e.kind == "txn" else MISSING

    ctx.drafts.put(Draft(
        token=token, action=action, entity=e.key, request=request, message=message, summary="\n".join(summary),
        reason=a["reason"], company_file=host.company_file, expires=time.time() + TOKEN_MINUTES * 60,
        source=source, allow_duplicate=a["allow_duplicate"], before=as_logged(before),
        total=total, warnings=warnings, checks=list(dict.fromkeys(checks)), **asked_by(c),
    ))
    return {
        "token": token,
        "expires_in_minutes": TOKEN_MINUTES,
        "preview": "\n".join(summary),
        **({"warnings": warnings} if warnings else {}),
        "company": facts.name,
        "next_step": "Show the preview (and any warnings) to the person. Run qbd commit with this token only after they approve it.",
    }


def as_logged(before: Any) -> Any:
    """A record's values before a change, as the write log keeps them: a field holding card, ID or payroll data is
    not kept at all, so it is never written to disk and never put back half masked."""
    if not isinstance(before, dict):
        return before
    masked = mask_personal_data(before)
    return {k: masked[k] if k in masked and masked[k] == v else NOT_KEPT for k, v in before.items()}


def refuse_saved_source(ctx: Ctx, company_file: str, source: str) -> None:
    e = committed(ctx.config.audit_log, company_file, source)
    if e is None:
        return
    if e.get("outcome") == "done":
        first = text(e.get("summary", "")).split("\n")[0]
        raise ToolError(f'A change from "{source}" was already saved on {e.get("time")}: {first} {dumps(e.get("ids", {}))}. '
                        "Set allow_duplicate if this is really a second change from it.")
    raise ToolError(f'A change from "{source}" was sent on {e.get("time")}, but QuickBooks never confirmed it. Search for the record; '
                    "set allow_duplicate only if it is not there.")


def _said(e: Exception) -> str:
    return str(e) if isinstance(e, (QbConnectionError, QbStatusError)) else f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- qb_import

LINE = "Line."


def _child(m: Model, name: str) -> str | None:
    """The element a column names: itself, or its reference when the column gives a plain name (Customer -> CustomerRef)."""
    low = {k.lower(): k for k in m.o}
    return low.get(name.lower()) or low.get(f"{name.lower()}ref")


def _put(out: dict, m: Model, path: list[str], value: str, column: str) -> None:
    name = _child(m, path[0])
    if name is None:
        raise ToolError(f'Column "{column}": there is no {path[0]} here. Fields: {", ".join(m.o)}')
    ref = m.c[name][0]
    if len(path) == 1:
        out[name] = value.lower() if ref == "b" and value.lower() in ("true", "false") else value
        return
    inner = out.setdefault(name, {})
    if not is_model(ref) or not isinstance(inner, dict):
        raise ToolError(f'Column "{column}": {name} has no {path[1]}.')
    _put(inner, model(ref), path[1:], value, column)


def _line_element(e: Entity, add: Model, kind: str, credit: bool, account: bool) -> tuple[str, Model]:
    if not kind:
        kind = (("JournalCreditLine" if credit else "JournalDebitLine") if e.base == "JournalEntry"
                else f"{e.base}Line" if f"{e.base}LineAdd" in add.c else "ExpenseLine" if account else "ItemLine")
    name = next((n for n in (f"{kind}Add", kind) if n in add.c), None)
    if name is None:
        raise ToolError(f'"{kind}" is not a kind of line here. Lines: {", ".join(k for k in add.o if "Line" in k)}')
    return name, model(add.c[name][0])


@tool(
    "qb_import",
    "Import records from a CSV file",
    "Check every record in a CSV file and draft them as one batch: invoices, bills, checks, journal entries, customers, "
    "vendors, items and the other types qbd draft can add. Columns are qbXML field names, as qbd export writes them: "
    "Customer (or CustomerRef), TxnDate, RefNumber, BillAddress.City..., and Line.Item, Line.Quantity, Line.Rate, "
    "Line.Account, Line.Amount... for line items (a LineKind column picks the kind of line; journal entries take "
    "Line.Account with Line.Debit or Line.Credit). Rows with the same RefNumber are one transaction. Each record is "
    "checked like qbd draft; records saved from this file before are skipped, so the file can be imported again after "
    "fixing the rows that failed. Returns one token for the batch: show the preview and commit it only after the person "
    "approves.",
    {
        "type": Arg("enum", "Record type, e.g. invoice, bill, journal_entry, customer", required=True, choices=WRITABLE),
        "csv": Arg("str", "The .csv file to read (UTF-8; dates like 2026-10-31)", required=True),
        "reason": Arg("str", "Why these records are entered; kept in the audit log with each one"),
        "limit": Arg("int", "Most records in one batch; import again for the rest", default=200, min=1, max=1000),
    },
    writes=True,
)
def import_csv(ctx: Ctx, a: dict) -> dict:
    e = entity(a["type"])
    if e.key not in ctx.config.write_entities:
        raise ToolError(f"Changing {e.key} records is not allowed here. Allowed: {', '.join(ctx.config.write_entities)} (QBD_WRITE_ENTITIES).")
    if not has_message(f"{e.base}AddRq"):
        raise ToolError(f"QuickBooks cannot add {e.key} records through the SDK.")
    path = Path(a["csv"])
    if path.suffix.lower() != ".csv" or not path.is_file():
        raise ToolError(f"No .csv file at {path}.")
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = [c.strip() for c in reader.fieldnames or [] if c and c.strip()]
            rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if isinstance(v, str)} for r in reader]
    except UnicodeDecodeError:
        raise ToolError("The file is not UTF-8. In Excel, save it as \"CSV UTF-8\".") from None
    add = model(message_model(f"{e.base}AddRq").c[f"{e.base}Add"][0])
    kind_col = next((c for c in columns if c.lower() == "linekind"), None)
    line_cols = [c for c in columns if c.startswith(LINE)]
    head_cols = [c for c in columns if c not in line_cols and c != kind_col]
    for c in head_cols:
        _put({}, add, c.split("."), "x", c)  # every column must name a field before anything is checked
    if line_cols and e.kind != "txn":
        raise ToolError(f"{e.key} records have no lines: remove the Line. columns.")
    ref_col = next((c for c in head_cols if c.lower() == "refnumber"), None) if e.kind == "txn" else None
    name_col = next((c for c in head_cols if c.lower() == "name"), None)
    groups: dict[str, list[tuple[int, dict]]] = {}
    for n, r in enumerate(rows, 2):  # row 1 is the header
        if any(r.values()):
            ref = r.get(ref_col, "") if ref_col else ""
            groups.setdefault(f"ref:{ref.lower()}" if ref else f"row:{n}", []).append((n, r))

    def fields_of(rows: list[tuple[int, dict]]) -> dict:
        fields: dict[str, Any] = {}
        for c in head_cols:
            values = list(dict.fromkeys(r[c] for _, r in rows if r.get(c)))
            if len(values) > 1:
                raise ToolError(f"These rows give different {c}: {', '.join(values)}.")
            if values:
                _put(fields, add, c.split("."), values[0], c)
        for _, r in rows:
            cells = {c[len(LINE):]: r[c] for c in line_cols if r.get(c)}
            if not cells and not r.get(kind_col or ""):
                continue
            debit, credit = cells.pop("Debit", ""), cells.pop("Credit", "")
            if debit and credit:
                raise ToolError("A row gives both Line.Debit and Line.Credit.")
            name, lm = _line_element(e, add, r.get(kind_col or "", ""), bool(credit), any(k.lower() in ("account", "accountref") for k in cells))
            line: dict[str, Any] = {"Amount": debit or credit} if debit or credit else {}
            for k, v in cells.items():
                _put(line, lm, k.split("."), v, LINE + k)
            fields.setdefault(name, []).append(line)
        return fields

    host = ctx.qb.host_info()
    log = ctx.config.audit_log
    ready: list[dict] = []
    skipped: list[dict] = []
    problems: list[dict] = []
    more = 0
    ctx.cache["refs"] = set()
    try:
        for group in groups.values():
            if len(ready) >= a["limit"]:
                more += 1
                continue
            first, last = group[0][0], group[-1][0]
            where = {"rows": f"{first}" if first == last else f"{first}-{last}"}
            ref = group[0][1].get(ref_col, "") if ref_col else ""
            name = group[0][1].get(name_col, "") if name_col else ""
            label = {"ref": ref} if ref else {"name": name} if name else {}
            source = f"{path.name}#{ref or name or f'row{first}'}"
            saved = committed(log, host.company_file, source)
            if saved is not None:
                why = (f"saved on {saved.get('time')}" if saved.get("outcome") == "done"
                       else f"sent on {saved.get('time')} but never confirmed: search for it before importing it again")
                skipped.append({**where, **label, "why": why})
                continue
            try:
                d = draft.run(ctx, parse_args(draft.input, {"action": "add", "type": e.key, "fields": fields_of(group), "source": source,
                                                            **({"reason": a["reason"]} if a["reason"] else {})}))
            except (ToolError, QbxmlInputError, InvalidArguments) as err:
                problems.append({**where, **label, "error": str(err)})
                continue
            ready.append({**where, **label, "token": d["token"], "preview": d["preview"], **({"warnings": d["warnings"]} if "warnings" in d else {})})
    finally:
        ctx.cache.pop("refs", None)

    counts = {"records": len(groups), "ready": len(ready), "skipped": len(skipped), "problems": len(problems), **({"not_read_yet": more} if more else {})}
    out: dict[str, Any] = {"file": str(path.resolve()), "type": e.key, "counts": counts}
    if ready:
        token = secrets.token_hex(4)
        preview = "\n".join([
            f"Add {len(ready)} {noun(e)} record{'s' if len(ready) > 1 else ''} from {path.name}:",
            *(f"- {r['preview'].splitlines()[0]}" for r in ready[:50]),
            *([f"- and {len(ready) - 50} more"] if len(ready) > 50 else []),
        ])
        warned = [f"{nz(r.get('ref'), r.get('name'), 'rows ' + r['rows'])}: {w}" for r in ready for w in r.get("warnings", [])]
        ctx.drafts.put(Draft(token=token, action="batch", entity=e.key, request=dumps([r["token"] for r in ready]), message="batch",
                             summary=preview, reason=a["reason"], company_file=host.company_file, expires=time.time() + TOKEN_MINUTES * 60,
                             warnings=warned[:20], **asked_by(ctx.config)))
        out.update({"token": token, "expires_in_minutes": TOKEN_MINUTES, "preview": preview, "company": company_facts(ctx).name})
    out.update({
        "ready": [{k: v for k, v in r.items() if k != "token"} for r in ready[:50]],
        **({"skipped": skipped[:50]} if skipped else {}),
        **({"problems": problems[:50]} if problems else {}),
        "next_step": "Show the preview, the warnings and the problems to the person. Run qbd commit with the token only after they "
                     "approve; fixed rows can be imported again later, and saved ones are skipped." if ready
        else "Nothing to save: fix the problems in the file and import it again.",
    })
    return out


def asked(d: Draft) -> dict[str, Any]:
    """Who asked for a draft, and its amount, as the write log keeps them."""
    return {**{k: v for k, v in (("agent", d.agent), ("session", d.session), ("program", d.program)) if v},
            **({"total": d.total} if d.total is not MISSING else {})}


def save(ctx: Ctx, d: Draft, approved_in: str = "chat") -> dict:
    """Send one checked draft to QuickBooks and log what happened."""
    if time.time() > d.expires:
        raise ToolError("The draft expired. Draft the change again so it is checked against current data.")
    host = ctx.qb.host_info()
    if host.company_file != d.company_file:
        raise ToolError(f"QuickBooks now has a different company file open ({host.company_file}). Draft again.")
    if d.source is not MISSING and not d.allow_duplicate:
        refuse_saved_source(ctx, d.company_file, d.source)  # another draft from the same source may have been saved since
    log = ctx.config.audit_log
    base = {"token": d.token, "companyFile": d.company_file, "action": d.action, "entity": d.entity, "summary": d.summary, "reason": d.reason,
            "source": d.source, **asked(d), **({"approved_in": approved_in} if approved_in != "chat" else {}), "before": d.before,
            "request": d.request}
    try:
        xml = ctx.qb.raw([d.request])
    except Exception as e:
        write_audit(log, {**base, "outcome": "unknown", "error": _said(e)})
        raise WriteUnconfirmed(f"QuickBooks did not confirm the change, so it may or may not be saved. Search for the record before trying again. ({_said(e)})") from e
    rs = next((r for r in parse_responses(xml) if r.request_id == d.token), None)
    if rs is None or rs.status_severity == "Error":
        err = QbStatusError(rs.status_code if rs else -1, rs.status_message if rs else "No answer from QuickBooks")
        write_audit(log, {**base, "outcome": "failed", "error": str(err)})
        raise err
    ret = nz(next((v for k, v in rs.body.items() if k.endswith("Ret")), MISSING), rs.body)
    ids = {k: get(ret, k) for k in ("TxnID", "ListID", "RefNumber", "FullName", "Name", "TxnNumber", "TimeDeleted") if get(ret, k) is not MISSING}
    write_audit(log, {**base, "outcome": "done", "ids": ids, "editSequence": get(ret, "EditSequence")})
    return {
        "done": True,
        "action": d.action,
        "type": d.entity,
        "summary": d.summary,
        "ids": ids,
        **({"warning": rs.status_message} if rs.status_severity == "Warn" else {}),
        "undo": f"qbd undo {d.token} drafts its deletion (deleting must be on)." if d.action == "add"
        else f"qbd undo {d.token} drafts the old values back." if d.action == "update"
        else "This cannot be undone from here; the write log keeps the record as it was, to re-enter it if it was a mistake.",
    }


def save_batch(ctx: Ctx, d: Draft, approved_in: str = "chat") -> dict:
    """Save the records of a qbd import one by one. A record QuickBooks refuses is reported and the rest go on; when
    QuickBooks stops answering, or a person stops it in the Activity Monitor, nothing more is sent."""
    if time.time() > d.expires:
        raise ToolError("The batch expired. Import the file again; the records saved from it are skipped.")
    results: list[dict] = []
    stop = ""
    for token in json.loads(d.request):
        child = ctx.drafts.take(token)
        if child is None:  # every record is accounted for, even one whose draft is gone
            results.append({"record": f"draft {token}", "outcome": "not_sent", "error": "Its draft is gone (it expired or was removed)"})
            continue
        record = child.summary.split("\n")[0]
        asked_stop = ctx.live.stop_asked() if ctx.live is not None and not stop else None
        if asked_stop is not None:
            stop = f"Stopped by {asked_stop.get('by') or 'someone at this PC'} in the Activity Monitor; the records saved before stay saved"
        if stop:
            results.append({"record": record, "outcome": "not_sent"})
            continue
        try:
            r = save(ctx, child, approved_in)
            results.append({"record": record, "outcome": "done", "ids": r["ids"], "token": child.token, **({"warning": r["warning"]} if "warning" in r else {})})
        except (WriteUnconfirmed, QbConnectionError) as err:
            stop = str(err)
            results.append({"record": record, "outcome": "unknown" if isinstance(err, WriteUnconfirmed) else "not_sent", "error": str(err)})
        except (ToolError, QbStatusError) as err:
            results.append({"record": record, "outcome": "failed", "error": str(err)})
    counts = {k: n for k in ("done", "failed", "unknown", "not_sent") if (n := sum(1 for r in results if r["outcome"] == k))}
    return {
        "done": counts.get("done", 0) == len(results),
        "type": d.entity,
        "counts": counts,
        "results": results,
        **({"stopped": stop + "." if stop.startswith("Stopped by") else f"QuickBooks stopped answering, so the rest was not sent: {stop}"} if stop else {}),
        "next_step": "Each saved record can be undone with qbd undo and its token. Fix the failed rows and import the file again: "
                     "the saved ones are skipped." if len(results) > counts.get("done", 0) else "All records are saved.",
    }


@tool(
    "qb_commit",
    "Save an approved draft to QuickBooks",
    "Send a change prepared by qbd draft, or a batch prepared by qbd import, to QuickBooks. Only run this after the person "
    "has seen the preview and said yes. Each token works once and expires after 15 minutes. When the person answered the "
    "draft in the Activity Monitor, this returns what they decided: what was saved, or that they rejected it.",
    {
        "token": Arg("str", "The token from qbd draft or qbd import", required=True),
        "approved_in": Arg("enum", "Where the person approved it; the Activity Monitor sets monitor when they approve it there",
                           default="chat", choices=("chat", "monitor")),
        "seen": Arg("str", "The draft's fingerprint as the person saw it. The Activity Monitor sends it, so that a draft "
                    "changed after it was shown is not saved"),
    },
    writes=True,
    destructive=True,
)
def commit(ctx: Ctx, a: dict) -> dict:
    in_monitor = a["approved_in"] == "monitor"
    if ctx.config.require_monitor_approval and not in_monitor and ctx.drafts.answered(a["token"]) is None:
        raise ToolError("On this PC, changes are approved in the Activity Monitor (QBD_REQUIRE_MONITOR_APPROVAL). The draft waits "
                        "there: tell the person it is ready, then run qbd commit again. Once they have approved it, that returns "
                        "what was saved.")
    if a["seen"] is not MISSING and (now := ctx.drafts.fingerprint(a["token"])) is not None and now != a["seen"]:
        raise ToolError("The draft changed after it was shown, so it was not saved. Look at it again before approving it.")
    d = ctx.drafts.take(a["token"])
    if d is None:
        return answered(ctx.drafts, a["token"])
    if ctx.live is not None and d.action != "batch":
        ctx.live.stoppable = False  # one change is sent whole: there is no step after which to stop
        ctx.live.update(stoppable=False)
    try:
        out = save_batch(ctx, d, a["approved_in"]) if d.action == "batch" else save(ctx, d, a["approved_in"])
    except (ToolError, QbStatusError, WriteUnconfirmed, QbConnectionError) as e:
        if in_monitor:
            ctx.drafts.answer(d.token, {"approved_in": "monitor", "by": who_am_i(), "at": _now(), "error": _said(e)})
        raise
    if in_monitor:
        ctx.drafts.answer(d.token, {"approved_in": "monitor", "by": who_am_i(), "at": _now(), "result": out})
    return out


def answered(drafts: Drafts, token: str) -> dict:
    """A commit of a draft that a person already answered in the Activity Monitor: what they decided."""
    answer = drafts.answered(token)
    if answer is None:
        raise ToolError("Unknown or already used token. Draft the change again.")
    by, at = answer.get("by") or "The person at this PC", answer.get("at")
    if answer.get("rejected"):
        raise ToolError(f"{by} rejected this change in the Activity Monitor ({at}), so nothing was saved. Ask what to change before "
                        "drafting it again.")
    if isinstance(answer.get("result"), dict):
        return {**answer["result"], "approved_in": "monitor",
                "note": f"{by} approved this change in the Activity Monitor ({at}) and it was saved then; it was not sent again."}
    raise ToolError(f"{by} approved this change in the Activity Monitor ({at}), but it was not saved: {answer.get('error')}")


def reject(config: Config, drafts: Drafts, token: str) -> dict:
    """A person said no to a draft in the Activity Monitor: it is deleted, with its records for a batch, and the write
    log and the draft's answer say so. Needs no QuickBooks."""
    d = drafts.take(token)
    if d is None:
        raise ToolError("This draft is gone: it was answered already, or it expired.")
    if d.action == "batch":
        for child in json.loads(d.request):
            drafts.take(child)
    entry = {"token": d.token, "companyFile": d.company_file, "action": d.action, "entity": d.entity, "summary": d.summary,
             "reason": d.reason, "source": d.source, **asked(d), "outcome": "rejected", "answered_in": "monitor"}
    write_audit(config.audit_log, entry)
    drafts.answer(d.token, {"rejected": True, "by": who_am_i(), "at": _now()})
    return entry


@tool(
    "qb_undo",
    "Draft the undo of a saved change",
    "Find a change saved through qbd commit in the write log by its token and draft its reverse: an added record is "
    "deleted (needs QBD_ALLOW_DELETE), an update puts the old values back. Deletes, voids, changes to lines or to card "
    "and ID data, and records changed again since are refused; the log keeps their old values for a person to redo. "
    "Returns a draft like qbd draft: commit it only after the person approves.",
    {"token": Arg("str", "The token of the saved change (qbd log lists them)", required=True)},
    writes=True,
)
def undo(ctx: Ctx, a: dict) -> dict:
    entry = next((x for x in read_audit(ctx.config.audit_log, 10 ** 9) if x.get("token") == a["token"] and x.get("outcome") == "done"), None)
    if entry is None:
        raise ToolError("No change saved with this token in the write log; qbd log lists the latest ones.")
    host = ctx.qb.host_info()
    if text(entry.get("companyFile", "")).lower() != host.company_file.lower():
        raise ToolError(f"That change was saved to another company file ({entry.get('companyFile')}). Open it in QuickBooks first.")
    action = entry.get("action")
    if action not in ("add", "update"):
        raise ToolError(f"A {action} cannot be undone from here. The write log keeps the record as it was (before): re-enter it if it was a mistake.")
    done = committed(ctx.config.audit_log, host.company_file, f"undo:{a['token']}")
    if done is not None:
        raise ToolError(f"This change was already undone on {done.get('time')} {dumps(done.get('ids', {}))}.")
    e = entity(text(entry.get("entity")))
    ids = nz(entry.get("ids"), {})
    id_ = nz(get(ids, "TxnID"), get(ids, "ListID"))
    now = find_existing(ctx, e, id_)
    if now is None:
        raise ToolError(f"The {e.key} is not in QuickBooks any more.")
    if truthy(entry.get("editSequence")) and text(get(now, "EditSequence")) != text(entry["editSequence"]):
        raise ToolError(f"The {e.key} was changed again (last on {text(get(now, 'TimeModified'))}) after this change: undo it by hand, or draft an update.")
    first = text(entry.get("summary", "")).split("\n")[0]
    args = {"id": id_, "reason": f"Undo of {a['token']}: {first}", "source": f"undo:{a['token']}"}
    if action == "add":
        return draft.run(ctx, parse_args(draft.input, {"action": "delete", "type": e.key, **args}))
    before = entry.get("before")
    if not isinstance(before, dict) or not before:
        raise ToolError("The write log does not keep the values before this change.")
    stuck = [k for k, v in before.items() if v == NOT_KEPT or re.search(r"Line(Group)?Ret\Z", k)]
    if stuck:
        raise ToolError(f"{', '.join(stuck)} cannot be put back from here (lines, or card and ID data the log does not keep): undo it by hand.")
    fields = {k: "" if v is None else v for k, v in before.items()}
    return draft.run(ctx, parse_args(draft.input, {"action": "update", "type": e.key, "fields": fields, **args}))


@tool(
    "qb_write_log",
    "Recent changes made through qbd",
    "The latest entries of the local audit log: every change committed or attempted, with who, when and QuickBooks' answer.",
    {"limit": Arg("int", default=20, min=1, max=200)},
    writes=True,
)
def write_log(ctx: Ctx, a: dict) -> dict:
    entries = read_audit(ctx.config.audit_log, a["limit"])
    return {"log_file": ctx.config.audit_log, "entries": [{k: v for k, v in x.items() if k != "request"} for x in entries]}


WRITE_TOOLS = [draft, import_csv, commit, undo, write_log]
