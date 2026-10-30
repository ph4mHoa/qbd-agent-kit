"""Read tools: status, search, get, reports, the schema browser and bounded raw queries."""
from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any

from ..errors import QbStatusError, ToolError
from ..jsutil import MISSING, get, is_num, num_str, nz, round2, text, truthy
from ..qb.catalog import Entity, compact, entities, entity, summary_fields
from ..qb.client import rets_of
from ..qb.dates import days_between, macro_range
from ..qbxml.build import LIST_ID
from ..qbxml.parse import Report
from ..qbxml.schema import enums, has_message, message_model, messages, model, supports_count
from ..safety.privacy import mask_personal_data
from .args import Arg
from .common import Ctx, company_facts, date, num, tool

ENTITY_KEYS = tuple(e.key for e in entities())
ENTITY_HELP = ", ".join(f"{e.key} ({e.label})" for e in entities())


def name_ref(s: str) -> dict:
    return {"ListIDWithChildren": s} if LIST_ID.match(s) else {"FullNameWithChildren": s}


def compact_deep(v: Any) -> Any:
    """{ListID, FullName} references become their name at every level; the rest is kept."""
    if isinstance(v, list):
        return [compact_deep(x) for x in v]
    if not isinstance(v, dict):
        return v
    out = {}
    for k, val in v.items():
        if k.endswith("Ref") and isinstance(val, dict):
            out[k[:-3]] = nz(get(val, "FullName"), get(val, "ListID"), get(val, "TxnID"))
        else:
            out[k] = compact_deep(val)
    return out


# ---------------------------------------------------------------- qb_status

# Intuit discontinues each US QuickBooks Desktop version on May 31, three years after its year: 2021 on 2024-05-31,
# 2022 on 2025-05-31 and 2023 on 2026-05-31 ("QuickBooks Desktop service discontinuation policy"). Later dates
# follow the same pattern until Intuit announces them. MajorVersion is the year less 1990 (34 is 2024).
LAST_ANNOUNCED_YEAR = 2023
DISCONTINUED = "no live support or security updates, and payroll, payments, bank feeds and emailing forms stop"


def intuit_support(host: Any, today: str) -> tuple[Any, list[str]]:
    major = host.version.split(".")[0]
    if host.country != "US" or not major.isdigit() or not 2000 <= int(major) + 1990 <= 2100:
        return MISSING, []
    year = int(major) + 1990
    ends = f"{year + 3}-05-31"
    left = days_between(today, ends)
    announced = year <= LAST_ANNOUNCED_YEAR
    # Intuit's wording: services are discontinued after May 31, so that day is the last one supported.
    if left < 0:
        return {"version_year": year, "ended": ends}, [
            f"Intuit stopped supporting QuickBooks Desktop {year} after {ends}: {DISCONTINUED}. Upgrading is a person's job: back up the company file first."]
    info = {"version_year": year, "ends": ends, "days_left": left, **({} if announced else {"announced": False})}
    if left > 180:
        return info, []
    return info, [f"Intuit {'stops' if announced else 'is expected to stop'} supporting QuickBooks Desktop {year} after {ends} "
                  f"({'today is the last day' if left == 0 else f'in {left} days'}): then {DISCONTINUED}. Plan the upgrade."]


@tool(
    "qb_status",
    "QuickBooks connection status",
    "Check the connection to QuickBooks Desktop and see which company file is open, the QuickBooks version and "
    "when Intuit stops supporting it, the closing date and whether changes are allowed. Run it first in a session.",
    {},
)
def status(ctx: Ctx, a: dict) -> dict:
    host = ctx.qb.host_info()
    facts = company_facts(ctx)
    c = ctx.config
    support, warnings = intuit_support(host, ctx.today())
    return {
        "connected": True,
        "demo": True if c.demo else MISSING,
        "company": facts.name,
        "company_file": host.company_file,
        "quickbooks": f"{host.product_name} {host.version}" + (f" ({host.country})" if host.country else ""),
        "qbxml_version": host.qbxml_version,
        "intuit_support": support,
        "today": ctx.today(),
        "closing_date": facts.closing_date,
        "fiscal_year_starts_month": facts.first_fiscal_month,
        "uses_inventory": facts.uses_inventory,
        "writes": {"enabled": True, "delete_and_void": c.allow_delete, "record_types": list(c.write_entities), "max_amount": c.max_amount}
        if c.allow_writes
        else {"enabled": False, "note": "Read-only. The owner can set QBD_ALLOW_WRITES=true to allow drafted changes."},
        **({"warnings": warnings} if warnings else {}),
    }


# ---------------------------------------------------------------- qb_profile

# The lists QuickBooks reports a maximum size for, and the queries that count their entries. Entity is customers,
# vendors, employees and other names together.
CAPACITY_LISTS = {
    "Entity": ["CustomerQueryRq", "VendorQueryRq", "EmployeeQueryRq", "OtherNameQueryRq"],
    "Item": ["ItemQueryRq"],
    "Account": ["AccountQueryRq"],
    "Class": ["ClassQueryRq"],
    "PriceLevel": ["PriceLevelQueryRq"],
    "SalesRep": ["SalesRepQueryRq"],
    "SalesTaxCode": ["SalesTaxCodeQueryRq"],
    "Terms": ["TermsQueryRq"],
    "Template": ["TemplateQueryRq"],
    "PaymentMethod": ["PaymentMethodQueryRq"],
    "ShipMethod": ["ShipMethodQueryRq"],
    "CustomerType": ["CustomerTypeQueryRq"],
    "VendorType": ["VendorTypeQueryRq"],
    "JobType": ["JobTypeQueryRq"],
    "CustomerMsg": ["CustomerMsgQueryRq"],
    "BillingRate": ["BillingRateQueryRq"],
    "PayrollItem": ["PayrollItemWageQueryRq", "PayrollItemNonWageQueryRq"],
    "ToDo": ["ToDoQueryRq"],
    "Vehicle": ["VehicleQueryRq"],
}


def _when(v: Any) -> Any:
    """A restore or condense time, or None for QuickBooks' "never" (the start of 1970 UTC, shown in local time)."""
    return None if not truthy(v) or str(v)[:4] < "1971" else v


@tool(
    "qb_profile",
    "Company file profile",
    "What this company file is like, in one call: the QuickBooks edition, which features are on (inventory, sites, "
    "multi-currency, classes, price levels, FIFO...), how full each list is against QuickBooks' limit, the custom fields, "
    "and when the file was last restored or condensed. Run it once at the start of a session; it takes a few seconds.",
    {},
)
def profile(ctx: Ctx, a: dict) -> dict:
    host = ctx.qb.host_info()
    facts = company_facts(ctx)
    warnings: list[str] = []

    def ask(message: str, body: dict, attrs: dict | None = None) -> Any:
        try:
            return ctx.qb.request(message, body, attrs)
        except QbStatusError as e:
            warnings.append(f"{message}: {e.message}")
            return None

    prefs = ask("PreferencesQueryRq", {})
    p = nz(get(prefs.body, "PreferencesRet"), {}) if prefs else {}
    acct, inv = nz(get(p, "AccountingPreferences"), {}), nz(get(p, "ItemsAndInventoryPreferences"), {})
    features = {
        "inventory": get(get(p, "PurchasesAndVendorsPreferences"), "IsUsingInventory"),
        "inventory_sites": get(get(p, "MultiLocationInventoryPreferences"), "IsMultiLocationInventoryEnabled"),
        "multi_currency": get(get(p, "MultiCurrencyPreferences"), "IsMultiCurrencyOn"),
        "home_currency": get(get(get(p, "MultiCurrencyPreferences"), "HomeCurrencyRef"), "FullName"),
        "class_tracking": get(acct, "IsUsingClassTracking"),
        "account_numbers": get(acct, "IsUsingAccountNumbers"),
        "audit_trail": get(acct, "IsUsingAuditTrail"),
        "estimates": get(get(p, "JobsAndEstimatesPreferences"), "IsUsingEstimates"),
        "progress_invoicing": get(get(p, "JobsAndEstimatesPreferences"), "IsUsingProgressInvoicing"),
        "price_levels": get(get(get(p, "SalesAndCustomersPreferences"), "PriceLevels"), "IsUsingPriceLevels"),
        "sales_tax": True if truthy(get(p, "SalesTaxPreferences")) else MISSING,
        "fifo": get(inv, "FIFOEnabled"),
        "fifo_since": get(inv, "FIFOEffectiveDate"),
        "serial_or_lot_numbers": get(inv, "IsTrackingSerialOrLotNumber"),
        "barcodes": get(inv, "IsBarcodeEnabled"),
    }
    rights = nz(get(p, "CurrentAppAccessRights"), {})

    host_rs = ask("HostQueryRq", {"IncludeListMetaData": {"IncludeMaxCapacity": True}})
    meta = nz(get(get(host_rs.body, "HostRet"), "ListMetaData"), {}) if host_rs else {}
    lists = []
    for key, value in (meta.items() if isinstance(meta, dict) else []):
        name = key[:-len("MetaData")] if key.endswith("MetaData") else key
        most = get(value, "MaxCapacity")
        count = 0
        for message in CAPACITY_LISTS.get(name, []):
            body = {"ActiveStatus": "All"} if "ActiveStatus" in message_model(message).c else {}
            rs = ask(message, body, {"metaData": "MetaDataOnly"})
            count = None if rs is None or count is None else count + int(rs.attrs.get("retCount", 0))
        row: dict[str, Any] = {"list": name, "max": most}
        if name in CAPACITY_LISTS and count is not None:
            row["count"] = count
            if truthy(most):
                row["used_percent"] = round(100 * count / most, 1)
        lists.append(row)
    lists.sort(key=lambda r: -r.get("used_percent", -1))

    defs = ask("DataExtDefQueryRq", {"OwnerID": "0"})
    custom = [
        {"name": r.get("DataExtName"), "type": r.get("DataExtType"), "on": nz(r.get("AssignToObject", MISSING), [])}
        for r in (rets_of(defs) if defs else [])
    ]
    act = ask("CompanyActivityQueryRq", {})
    activity = nz(get(act.body, "CompanyActivityRet"), {}) if act else {}
    return {
        "company": facts.name,
        "quickbooks": f"{host.product_name} {host.version}" + (f" ({host.country})" if host.country else ""),
        "file_mode": host.file_mode,
        "closing_date": facts.closing_date,
        "fiscal_year_starts_month": facts.first_fiscal_month,
        "features": {k: v for k, v in features.items() if v is not MISSING},
        "app_access": {"personal_data": get(rights, "IsPersonalDataAccessAllowed"), "automatic_login": get(rights, "IsAutomaticLoginAllowed")},
        "lists": lists,
        "custom_fields": custom,
        "last_restored": _when(get(activity, "LastRestoreTime")),
        "last_condensed": _when(get(activity, "LastCondenseTime")),
        **({"warnings": warnings} if warnings else {}),
    }


# ---------------------------------------------------------------- qb_search

MATCH = {"contains": "Contains", "starts_with": "StartsWith", "ends_with": "EndsWith"}
ACTIVE = {"active": "ActiveOnly", "inactive": "InactiveOnly", "all": "All"}


def search_body(e: Entity, a: dict) -> dict:
    m = message_model(e.query)

    def has(k: str) -> bool:
        return k in m.c

    def refuse(arg: str, why: str) -> None:
        raise ToolError(f"{arg} cannot be used when searching {e.key}: {why}")

    body: dict[str, Any] = {}
    if e.kind == "list":
        for k in ("ref_number", "party", "account", "from", "to", "status", "txn_types"):
            if a[k] is not MISSING:
                refuse(k, "it is a transaction filter; lists take name, active, modified_since and account_type")
        if a["name"] and a["match"] == "exact":
            body["FullName" if has("FullName") else "ListID"] = a["name"]
            return body
        body["ActiveStatus"] = ACTIVE[a["active"]]
        if a["name"]:
            body["NameFilter"] = {"MatchCriterion": MATCH[a["match"]], "Name": a["name"]}
        if a["modified_since"]:
            body["FromModifiedDate"] = f"{a['modified_since']}T00:00:00"
        if a["account_type"]:
            if not has("AccountType"):
                refuse("account_type", "only accounts have an account type")
            body["AccountType"] = a["account_type"]
        return body
    if a["name"]:
        refuse("name", "transactions have no name; use party for the customer or vendor, or ref_number")
    if a["account_type"]:
        refuse("account_type", "use account for transactions")
    if (a["from"] or a["to"]) and a["modified_since"]:
        refuse("modified_since", "QuickBooks filters by transaction date or by change date, not both")
    if e.key == "transaction":
        if a["ref_number"]:
            return {"RefNumber": a["ref_number"]}
        if a["from"] or a["to"]:
            body["TransactionDateRangeFilter"] = {"FromTxnDate": a["from"], "ToTxnDate": a["to"]}
        elif a["modified_since"]:
            body["TransactionModifiedDateRangeFilter"] = {"FromModifiedDate": f"{a['modified_since']}T00:00:00"}
        if a["party"]:
            body["TransactionEntityFilter"] = name_ref(a["party"])
        if a["account"]:
            body["TransactionAccountFilter"] = name_ref(a["account"])
        if a["txn_types"]:
            body["TransactionTypeFilter"] = {"TxnTypeFilter": a["txn_types"]}
        if a["status"] and a["status"] != "all":
            body["TransactionPaidStatusFilter"] = "Open" if a["status"] == "open" else "Closed"
        return body
    if a["txn_types"] is not MISSING:
        refuse("txn_types", "it only applies to type 'transaction'")
    if a["ref_number"]:
        if not has("RefNumber"):
            refuse("ref_number", "this record type has no reference number")
        body["RefNumber"] = a["ref_number"]
    else:
        if a["from"] or a["to"]:
            body["TxnDateRangeFilter"] = {"FromTxnDate": a["from"], "ToTxnDate": a["to"]}
        elif a["modified_since"]:
            body["ModifiedDateRangeFilter"] = {"FromModifiedDate": f"{a['modified_since']}T00:00:00"}
        if a["party"]:
            if not has("EntityFilter"):
                refuse("party", "QuickBooks cannot filter this record type by name")
            body["EntityFilter"] = name_ref(a["party"])
        if a["account"]:
            if not has("AccountFilter"):
                refuse("account", "QuickBooks cannot filter this record type by account")
            body["AccountFilter"] = name_ref(a["account"])
        if a["status"] and a["status"] != "all":
            if not has("PaidStatus"):
                refuse("status", "only invoices, bills and statement charges have a paid status")
            body["PaidStatus"] = "NotPaidOnly" if a["status"] == "open" else "PaidOnly"
    if a["include_lines"] and has("IncludeLineItems"):
        body["IncludeLineItems"] = True
    return body


# The filters qbd search and qbd count share.
FILTERS = {
    "name": Arg("str", "Lists: text in the name. Jobs are 'Customer:Job'."),
    "match": Arg("enum", "How name matches", default="contains", choices=("contains", "starts_with", "ends_with", "exact")),
    "ref_number": Arg("str", "Transactions: exact reference number, e.g. invoice number"),
    "party": Arg("str", "Transactions: customer, vendor or payee full name; a customer includes its jobs"),
    "account": Arg("str", "Transactions: account full name"),
    "from": date("Transactions dated on or after"),
    "to": date("Transactions dated on or before"),
    "modified_since": date("Only records changed on or after this date"),
    "status": Arg("enum", "Invoices, bills, statement charges (and type 'transaction'): open or paid", choices=("open", "paid", "all")),
    "active": Arg("enum", "Lists: which records", default="active", choices=("active", "inactive", "all")),
    "account_type": Arg("enum", "Accounts only, e.g. Bank, Income, Expense", choices=tuple(enums()["AccountType"])),
    "txn_types": Arg("list", "Type 'transaction' only: which transaction types", choices=tuple(enums()["TxnType"])),
}


@tool(
    "qb_search",
    "Search QuickBooks records",
    "Find customers, vendors, items, accounts or transactions (invoices, bills, payments, orders, checks and more) with simple "
    "filters. Returns one compact row per record; use qbd get for one record with its lines. Without from/to, QuickBooks picks "
    "which transactions come first (often the oldest), so pass dates when you want recent ones. "
    "When `truncated` is true more records match: narrow the filters (dates, party, name) instead of raising the limit.",
    {
        "type": Arg("enum", f"What to search: {ENTITY_HELP}", required=True, choices=ENTITY_KEYS),
        **FILTERS,
        "fields": Arg("list", "Extra fields to return, by qbXML name, e.g. ShipAddress (see qbd schema)"),
        "include_lines": Arg("bool", "Transactions: include line items", default=False),
        "limit": Arg("int", "Most records to return", default=25, min=1, max=1000),
    },
)
def search(ctx: Ctx, a: dict) -> dict:
    e = entity(a["type"])
    body = search_body(e, a)
    extra = a["fields"] if a["fields"] is not MISSING else []
    unknown = [f for f in extra if f not in e.fields]
    if unknown:
        raise ToolError(f"{e.key} has no field {', '.join(unknown)}. Its fields: {', '.join(e.fields)}")
    line_fields = [f for f in e.fields if re.search(r"Line(Group)?Ret\Z", f)] if a["include_lines"] else []
    show = list(dict.fromkeys([*summary_fields(e), *extra, *line_fields]))
    if "IncludeRetElement" in message_model(e.query).c:
        body["IncludeRetElement"] = [f for f in show if f != "_type"]
    res = ctx.qb.query(e.query, body, a["limit"])
    rows = [compact_deep(compact(r)) for r in res.rows]
    if e.kind == "txn":
        rows.sort(key=lambda r: text(nz(get(r, "TxnDate"), "")), reverse=True)
    return {
        "type": e.key,
        "count": len(rows),
        "truncated": res.truncated,
        **({"more": res.remaining} if truthy(res.remaining) else {}),
        **({"hint": "More records match. Narrow the search (dates, party, name) rather than raising the limit."} if res.truncated else {}),
        **({"warnings": res.warnings} if res.warnings else {}),
        "rows": rows,
    }


# ---------------------------------------------------------------- qb_export

LINES = re.compile(r"Line(Group)?Ret\Z")


def flatten(v: Any, prefix: str = "") -> dict:
    """One CSV cell per value: nested fields become Parent.Child, lists of plain values are joined, other lists are JSON."""
    out: dict[str, Any] = {}
    for k, x in v.items():
        key = f"{prefix}{k}"
        if isinstance(x, dict):
            out.update(flatten(x, f"{key}."))
        elif isinstance(x, list):
            out[key] = "; ".join(map(str, x)) if all(not isinstance(y, (dict, list)) for y in x) else json.dumps(x, ensure_ascii=False)
        else:
            out[key] = "true" if x is True else "false" if x is False else num_str(x) if is_num(x) else x
    return out


@tool(
    "qb_export",
    "Export records to a CSV file",
    "Write customers, vendors, items, accounts or transactions to a CSV file for Excel, Power BI or another program, with "
    "the same filters as qbd search: one row per record, or one row per line item with the record's fields repeated. "
    "Records are read in pages, so large exports are safe. The answer is a short summary; the rows go to the file.",
    {
        "type": Arg("enum", f"What to export: {ENTITY_HELP}", required=True, choices=ENTITY_KEYS),
        **FILTERS,
        "fields": Arg("list", "Extra fields, by qbXML name, e.g. ShipAddress (see qbd schema)"),
        "lines": Arg("bool", "Transactions: one row per line item", default=False),
        "csv": Arg("str", "The .csv file to write; it must not exist yet", required=True),
        "limit": Arg("int", "Most records", default=10000, min=1, max=200000),
    },
)
def export(ctx: Ctx, a: dict) -> dict:
    path = Path(a["csv"])
    if path.suffix.lower() != ".csv":
        raise ToolError("csv must name a .csv file.")
    if path.exists():
        raise ToolError(f"{path} already exists: give another file name.")
    e = entity(a["type"])
    if a["lines"] and e.kind != "txn":
        raise ToolError("lines is for transactions only.")
    body = search_body(e, {**a, "include_lines": a["lines"]})
    extra = a["fields"] if a["fields"] is not MISSING else []
    unknown = [f for f in extra if f not in e.fields]
    if unknown:
        raise ToolError(f"{e.key} has no field {', '.join(unknown)}. Its fields: {', '.join(e.fields)}")
    show = list(dict.fromkeys([*summary_fields(e), *extra, *([f for f in e.fields if LINES.search(f)] if a["lines"] else [])]))
    if "IncludeRetElement" in message_model(e.query).c:
        body["IncludeRetElement"] = [f for f in show if f != "_type"]
    res = ctx.qb.query(e.query, body, a["limit"], page_size=500)
    records = [compact_deep(compact(r)) for r in res.rows]
    if ctx.config.mask_personal_data:
        records = mask_personal_data(records)
    rows: list[dict] = []
    for r in records:
        head = flatten({k: v for k, v in r.items() if not LINES.search(k)})
        lines = [(k[:-3], line) for k, v in r.items() if LINES.search(k) for line in (v if isinstance(v, list) else [v])]
        rows += [{**head, "LineKind": kind, **flatten(line, "Line.")} for kind, line in lines] or [head]
    columns = list(dict.fromkeys(k for row in rows for k in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", newline="", encoding="utf-8-sig") as f:  # the byte-order mark makes Excel read UTF-8
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    return {
        "type": e.key,
        "file": str(path.resolve()),
        "records": len(records),
        "rows": len(rows),
        "columns": columns,
        **({"truncated": True, "hint": f"Only the first {a['limit']} records were written. Raise limit or narrow the filters."} if res.truncated else {}),
        **({"warnings": res.warnings} if res.warnings else {}),
    }


# ---------------------------------------------------------------- qb_count


@tool(
    "qb_count",
    "Count QuickBooks records",
    "Count customers, items, invoices or any other record type without reading the records, with the same filters as qbd "
    "search. QuickBooks counts them itself: most counts take under a second even on large files, but deposits and journal "
    "entries can take 5-20 seconds. QuickBooks calls the count approximate while others are changing records. Lists count "
    "active records unless active is given.",
    {
        "type": Arg("enum", f"What to count: {ENTITY_HELP}", required=True, choices=ENTITY_KEYS),
        **FILTERS,
    },
)
def count(ctx: Ctx, a: dict) -> dict:
    e = entity(a["type"])
    rs = ctx.qb.request(e.query, search_body(e, {**a, "include_lines": False}), {"metaData": "MetaDataOnly"})
    return {"type": e.key, "count": int(rs.attrs.get("retCount", 0))}


# ---------------------------------------------------------------- qb_changes

SINCE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2})?)?\Z")
CHANGE_LISTS = {"customer": "CustomerQueryRq", "vendor": "VendorQueryRq", "item": "ItemQueryRq", "account": "AccountQueryRq"}


@tool(
    "qb_changes",
    "What changed since a time",
    "Transactions and list records (customers, vendors, items, accounts) added or changed since a date or time, and records "
    "deleted since then, newest first. Transactions dated on or before the closing date are flagged: someone changed a "
    "closed period. Pass next_since to the following call to pick up where this one ended (records changed in that same "
    "second come back again). QuickBooks keeps deletions for about 90 days, and a company file restored from a backup since "
    "then is reported.",
    {
        "since": Arg("str", "Date YYYY-MM-DD or date-time YYYY-MM-DDThh:mm:ss, in the QuickBooks computer's time", required=True),
        "limit": Arg("int", "Most transactions, and most records per list", default=100, min=1, max=1000),
    },
)
def changes(ctx: Ctx, a: dict) -> dict:
    if not SINCE.match(a["since"]):
        raise ToolError("since must be a date (YYYY-MM-DD) or a date-time (YYYY-MM-DDThh:mm:ss).")
    since = a["since"] if "T" in a["since"] else f"{a['since']}T00:00:00"
    closing = company_facts(ctx).closing_date
    warnings: list[str] = []
    truncated = False
    stamps: list[str] = []

    def new(r: dict) -> bool:
        return text(nz(r.get("TimeCreated", MISSING), ""))[:19] >= since[:19]

    res = ctx.qb.query("TransactionQueryRq", {
        "TransactionModifiedDateRangeFilter": {"FromModifiedDate": since},
        "IncludeRetElement": ["TxnType", "TxnID", "TimeCreated", "TimeModified", "EntityRef", "TxnDate", "RefNumber", "Amount"],
    }, a["limit"])
    truncated |= res.truncated
    txns = []
    for r in res.rows:
        stamps.append(text(r.get("TimeModified", "")))
        row = {"type": r.get("TxnType"), "id": r.get("TxnID"), "ref": r.get("RefNumber", MISSING), "date": r.get("TxnDate"),
               "party": get(r.get("EntityRef", MISSING), "FullName"), "amount": r.get("Amount", MISSING),
               "change": "added" if new(r) else "changed", "modified": r.get("TimeModified")}
        if closing and text(r.get("TxnDate", "")) <= closing:
            row["closed_period"] = True
        txns.append({k: v for k, v in row.items() if v is not MISSING})
    txns.sort(key=lambda r: text(r["modified"]), reverse=True)

    records = []
    for key, message in CHANGE_LISTS.items():
        res = ctx.qb.query(message, {"ActiveStatus": "All", "FromModifiedDate": since,
                                     "IncludeRetElement": ["ListID", "FullName", "Name", "IsActive", "TimeCreated", "TimeModified"]}, a["limit"])
        truncated |= res.truncated
        for r in res.rows:
            stamps.append(text(r.get("TimeModified", "")))
            records.append({"list": key, "id": r.get("ListID"), "name": nz(r.get("FullName", MISSING), r.get("Name", MISSING)),
                            "active": r.get("IsActive", MISSING), "change": "added" if new(r) else "changed", "modified": r.get("TimeModified")})
    records.sort(key=lambda r: text(r["modified"]), reverse=True)

    deleted = []
    for message, kind, key, label in (("TxnDeletedQueryRq", "transaction", "TxnDelType", "RefNumber"),
                                      ("ListDeletedQueryRq", "list", "ListDelType", "FullName")):
        try:
            rs = ctx.qb.request(message, {key: enums()[key], "DeletedDateRangeFilter": {"FromDeletedDate": since}})
        except QbStatusError as e:
            warnings.append(f"{message}: {e.message}")
            continue
        for r in rets_of(rs):
            stamps.append(text(r.get("TimeDeleted", "")))
            deleted.append({"kind": kind, "type": r.get(key), "id": nz(r.get("TxnID", MISSING), r.get("ListID", MISSING)),
                            "ref" if kind == "transaction" else "name": r.get(label, MISSING), "deleted": r.get("TimeDeleted")})
    deleted.sort(key=lambda r: text(r["deleted"]), reverse=True)

    activity = nz(get(ctx.qb.request("CompanyActivityQueryRq", {}).body, "CompanyActivityRet"), {})
    restored = _when(get(activity, "LastRestoreTime"))
    if restored and text(restored)[:19] >= since[:19]:
        warnings.append(f"The company file was restored from a backup at {restored}: changes made after that backup was "
                        "taken and before the restore are gone, so read the records again instead of relying on this list.")
    closed = sum(1 for t in txns if t.get("closed_period"))
    return {
        "since": since,
        "closing_date": closing,
        "transactions": txns,
        **({"closed_period_changes": closed} if closed else {}),
        "records": records,
        "deleted": [{k: v for k, v in d.items() if v is not MISSING} for d in deleted],
        **({"next_since": max(stamps)[:19]} if stamps and not truncated else {}),
        **({"truncated": True, "hint": "More changes than the limit. Raise limit or ask for a later since."} if truncated else {}),
        **({"warnings": warnings} if warnings else {}),
    }


# ---------------------------------------------------------------- qb_snapshot

# What a snapshot counts: lists with inactive records, and the transaction types most files use.
SNAPSHOT_LISTS = ("account", "customer", "vendor", "employee", "other_name", "item")
SNAPSHOT_TXNS = ("invoice", "sales_receipt", "credit_memo", "receive_payment", "deposit", "estimate", "sales_order", "bill",
                 "vendor_credit", "bill_payment_check", "bill_payment_credit_card", "check", "credit_card_charge",
                 "credit_card_credit", "purchase_order", "item_receipt", "journal_entry", "inventory_adjustment",
                 "build_assembly", "transfer", "sales_tax_payment")


def take_snapshot(ctx: Ctx) -> dict:
    host = ctx.qb.host_info()
    res = ctx.qb.query("AccountQueryRq", {"ActiveStatus": "All", "IncludeRetElement": ["FullName", "Balance"]}, 10_000, page_size=500)
    counts: dict[str, int] = {}
    not_counted: list[str] = []
    for key in (*SNAPSHOT_LISTS, *SNAPSHOT_TXNS):
        e = entity(key)
        try:
            rs = ctx.qb.request(e.query, {"ActiveStatus": "All"} if key in SNAPSHOT_LISTS else {}, {"metaData": "MetaDataOnly"})
        except QbStatusError:
            not_counted.append(key)
            continue
        counts[key] = int(rs.attrs.get("retCount", 0))
    return {
        "snapshot": 1,
        "company": company_facts(ctx).name,
        "company_file": host.company_file,
        "quickbooks": f"{host.product_name} {host.version}",
        "taken": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "accounts": {text(r.get("FullName")): num(r.get("Balance")) for r in res.rows},
        "counts": counts,
        **({"not_counted": not_counted} if not_counted else {}),
        **({"truncated": True} if res.truncated else {}),
    }


def read_snapshot(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            saved = json.load(f)
    except FileNotFoundError:
        raise ToolError(f"No file at {path}.") from None
    except (OSError, ValueError) as e:
        raise ToolError(f"{path} cannot be read as JSON: {e}") from None
    if not isinstance(saved, dict) or saved.get("snapshot") != 1 or not isinstance(saved.get("accounts"), dict) or not isinstance(saved.get("counts"), dict):
        raise ToolError(f"{path} is not a snapshot: save one with qbd snapshot --out {path}.")
    return saved


@tool(
    "qb_snapshot",
    "Balances and record counts, to compare later",
    "The company file's control totals: every account's balance and how many customers, vendors, items, invoices, "
    "bills and other records it holds. Save one before a Rebuild, an upgrade, a condense or a big import (qbd snapshot "
    "--out before.json), then compare after (qbd snapshot --compare before.json): it lists the balances and counts that "
    "moved, so a person can see that only what should change did. Read-only.",
    {"compare": Arg("str", "A snapshot saved earlier with --out: show what moved since")},
)
def snapshot(ctx: Ctx, a: dict) -> dict:
    before = read_snapshot(a["compare"]) if a.get("compare") else None
    now = take_snapshot(ctx)
    if before is None:
        return now
    if before.get("company") != now["company"]:
        raise ToolError(f"The snapshot is of {before.get('company')}, and QuickBooks has {now['company']} open: compare snapshots of the same company.")
    accounts = []
    for name in sorted({*before["accounts"], *now["accounts"]}):
        b, n = before["accounts"].get(name, MISSING), now["accounts"].get(name, MISSING)
        if b is MISSING or n is MISSING or round2(n - b):
            accounts.append({"account": name, "before": b, "after": n, **({"change": round2(n - b)} if b is not MISSING and n is not MISSING else {})})
    accounts.sort(key=lambda r: -abs(r.get("change", 0)) if "change" in r else float("-inf"))
    kinds = (*SNAPSHOT_LISTS, *SNAPSHOT_TXNS)
    counts = [{"type": k, "before": before["counts"][k], "after": now["counts"][k], "change": now["counts"][k] - before["counts"][k]}
              for k in kinds if k in before["counts"] and k in now["counts"] and before["counts"][k] != now["counts"][k]]
    # QuickBooks refused a count in one of the two (not_counted): a missing count is not a deletion.
    one_side = [k for k in kinds if (k in before["counts"]) != (k in now["counts"])]
    notes = []
    if before.get("company_file") != now["company_file"]:
        notes.append(f"The company file was {before.get('company_file')} and is now {now['company_file']}.")
    if before.get("quickbooks") != now["quickbooks"]:
        notes.append(f"QuickBooks was {before.get('quickbooks')} and is now {now['quickbooks']}.")
    if one_side:
        notes.append(f"Counted in only one of the two snapshots, so not compared: {', '.join(one_side)}.")
    if not accounts and not counts:
        notes.append("Every balance and count is the same.")
    elif not accounts:
        notes.append("Every balance is the same; only record counts changed.")
    else:
        notes.append("Balances changed: records were added, changed or deleted between the snapshots, or the work done in "
                     "between (a Rebuild, an import) changed them. Around a Rebuild or an upgrade, keep the backup taken "
                     "before, and show this to your accountant.")
    return {
        "before": {k: before.get(k, MISSING) for k in ("taken", "quickbooks")},
        "after": {k: now[k] for k in ("taken", "quickbooks")},
        "same": not accounts and not counts,
        "balances": accounts,
        "counts": counts,
        "notes": notes,
        **({"next_step": f"qbd changes {before['taken']} lists what was added, changed or deleted since the first snapshot."}
           if (accounts or counts) and isinstance(before.get("taken"), str) else {}),
    }


# ---------------------------------------------------------------- qb_get


@tool(
    "qb_get",
    "Get one QuickBooks record",
    "Read one record in full: a transaction with its line items and linked transactions, or a list record such as a "
    "customer with addresses and terms. Give id (ListID or TxnID), or name for lists, or ref_number for transactions.",
    {
        "type": Arg("enum", f"Record type: {ENTITY_HELP}", required=True, choices=ENTITY_KEYS),
        "id": Arg("str", "ListID or TxnID from a search"),
        "name": Arg("str", "Lists: exact full name"),
        "ref_number": Arg("str", "Transactions: reference number (several may match)"),
    },
)
def get_one(ctx: Ctx, a: dict) -> dict:
    e = entity(a["type"])
    m = message_model(e.query)
    body: dict[str, Any] = {}
    if a["id"]:
        body["ListID" if e.kind == "list" else "TxnID"] = a["id"]
    elif a["name"] and e.kind == "list":
        body["FullName" if "FullName" in m.c else "ListID"] = a["name"]
    elif a["ref_number"] and e.kind == "txn" and "RefNumber" in m.c:
        body["RefNumber"] = a["ref_number"]
    else:
        raise ToolError("Give id or name." if e.kind == "list" else "Give id or ref_number.")
    if "IncludeLineItems" in m.c:
        body["IncludeLineItems"] = True
    if "IncludeLinkedTxns" in m.c:
        body["IncludeLinkedTxns"] = True
    if "OwnerID" in m.c:
        body["OwnerID"] = "0"  # custom fields
    res = ctx.qb.query(e.query, body, 5)
    if not res.rows:
        return {"type": e.key, "found": False, "note": "No record matches. Search first with qbd search."}
    return {"type": e.key, "found": True, "records": [compact_deep(r) for r in res.rows]}


# ---------------------------------------------------------------- qb_report

ALIASES = {
    "profit_and_loss": "ProfitAndLossStandard",
    "profit_and_loss_by_class": "ProfitAndLossByClass",
    "profit_and_loss_by_job": "ProfitAndLossByJob",
    "profit_and_loss_prev_year": "ProfitAndLossPrevYearComp",
    "profit_and_loss_detail": "ProfitAndLossDetail",
    "balance_sheet": "BalanceSheetStandard",
    "balance_sheet_prev_year": "BalanceSheetPrevYearComp",
    "trial_balance": "TrialBalance",
    "ar_aging": "ARAgingSummary",
    "ar_aging_detail": "ARAgingDetail",
    "ap_aging": "APAgingSummary",
    "ap_aging_detail": "APAgingDetail",
    "collections": "CollectionsReport",
    "customer_balances": "CustomerBalanceSummary",
    "vendor_balances": "VendorBalanceSummary",
    "open_invoices": "OpenInvoices",
    "unpaid_bills": "UnpaidBillsDetail",
    "sales_by_customer": "SalesByCustomerSummary",
    "sales_by_item": "SalesByItemSummary",
    "sales_by_rep": "SalesByRepSummary",
    "expenses_by_vendor": "ExpenseByVendorSummary",
    "purchases_by_vendor": "PurchaseByVendorSummary",
    "purchases_by_item": "PurchaseByItemSummary",
    "inventory_valuation": "InventoryValuationSummary",
    "inventory_stock_status": "InventoryStockStatusByItem",
    "sales_tax_liability": "SalesTaxLiability",
    "open_sales_orders": "OpenSalesOrderByCustomer",
    "open_purchase_orders": "OpenPOs",
    "transactions_by_date": "TxnListByDate",
    "general_ledger": "GeneralLedger",
    "journal": "Journal",
    "audit_trail": "AuditTrail",
    "job_profitability": "JobProfitabilitySummary",
    "budget_vs_actual": "ProfitAndLossBudgetVsActual",
    "time_by_job": "TimeByJobSummary",
}

# Through the SDK the Audit Trail stopped QuickBooks Enterprise 24 with an unrecoverable error (30674 10300) whenever it
# had rows to send; QuickBooks' own log shows the report built, then an exception in its SDK layer. Its default columns
# answer 510, and its dates pick transaction dates while "Entered/Last Modified" stays at today.
AUDIT_TRAIL_OFF = ("The Audit Trail report is not run from here: through the SDK it stopped QuickBooks with an unrecoverable "
                   "error. qbd changes <since> lists what was added, changed or deleted; or open Reports > Accountant & "
                   "Taxes > Audit Trail in QuickBooks.")
# For every item at once, the Inventory Valuation Summary stopped QuickBooks Enterprise 24 (heap corruption after a
# 47-second build on a large file). For one item it stays small.
VALUATION_ONE_ITEM = ("The Inventory Valuation Summary is run from here for one item at a time (give item): for every item "
                      "at once it stopped QuickBooks with an unrecoverable error. The total value of the stock is the "
                      "Inventory Asset line of qbd report balance_sheet; or open Reports > Inventory > Inventory Valuation "
                      "Summary in QuickBooks.")


FAMILIES = [
    ("GeneralSummaryReportType", "GeneralSummaryReportQueryRq"),
    ("GeneralDetailReportType", "GeneralDetailReportQueryRq"),
    ("AgingReportType", "AgingReportQueryRq"),
    ("BudgetSummaryReportType", "BudgetSummaryReportQueryRq"),
    ("JobReportType", "JobReportQueryRq"),
    ("TimeReportType", "TimeReportQueryRq"),
]


def resolve_report(name: str) -> dict[str, Any]:
    type_ = ALIASES.get(name, name)
    for family, message in FAMILIES:
        if type_ in enums().get(family, []):
            detail = family == "GeneralDetailReportType" or type_.endswith("Detail")
            return {"type": type_, "family": family, "message": message, "detail": detail}
    examples = ", ".join(enums()["GeneralSummaryReportType"][:4])
    raise ToolError(f'Unknown report "{name}". Use one of: {", ".join(ALIASES)}; or a QuickBooks report type such as {examples}.')


def render_report(r: Report, max_rows: int) -> dict:
    label_col = next((i for i, c in enumerate(r.columns) if c.type == "Label"), -1)
    rows = []
    for row in r.rows[:max_rows]:
        label = "  " * row.depth + row.label
        if row.kind == "text":
            rows.append([label])
            continue
        values = [label if i == label_col else v for i, v in enumerate(row.values)] if label_col >= 0 else list(row.values)
        while len(values) > 1 and (values[-1] is None or values[-1] == ""):
            values.pop()
        if all(v is None or v == "" for v in values):
            continue  # nothing in it: an empty report (an Audit Trail day without changes) sends one such row
        rows.append(values)
    return {
        "title": r.title,
        "period": r.subtitle,
        **({"basis": r.basis} if truthy(r.basis) else {}),
        "columns": [c.title or ("" if i == label_col else c.type) for i, c in enumerate(r.columns)],
        "rows": rows,
        **({"truncated": True, "total_rows": len(r.rows)} if len(r.rows) > max_rows else {}),
    }


def run_report(ctx: Ctx, a: dict) -> Report:
    r = resolve_report(a["report"])
    if r["type"] == "AuditTrail":
        raise ToolError(AUDIT_TRAIL_OFF)
    if r["type"] == "InventoryValuationSummary" and not a["item"]:
        raise ToolError(VALUATION_ONE_ITEM)
    m = message_model(r["message"])
    body: dict[str, Any] = {r["family"]: r["type"]}
    if a["from"] or a["to"]:
        to = a["to"] or ctx.today()
        as_of = r["family"] == "AgingReportType" or re.search(r"BalanceSheet|InventoryValuationSummary", r["type"]) is not None
        from_ = a["from"] or (to if as_of else MISSING)
        if not from_:
            raise ToolError("Give from as well as to, or use period.")
        if r["detail"] and days_between(from_, to) > 366:
            raise ToolError("Detail reports are limited to one year at a time. Narrow from/to.")
        body["ReportPeriod"] = {"FromReportDate": from_, "ToReportDate": to}
    else:
        period = nz(a["period"], "Today" if r["family"] == "AgingReportType" else "ThisMonthToDate")
        if r["detail"] and period == "All":
            raise ToolError("Detail reports need a period; 'All' would read the whole file.")
        # Periods go out as dates: QuickBooks refuses some period names its own schema lists (ThisCalendarQuarterToDate).
        rng = None if period == "All" else macro_range(period, ctx.today(), company_facts(ctx).first_fiscal_month)
        if rng:
            body["ReportPeriod"] = {"FromReportDate": rng["from"], "ToReportDate": rng["to"]}
        else:
            body["ReportDateMacro"] = period
    if a["basis"]:
        if "ReportBasis" not in m.c:
            raise ToolError(f"{r['type']} has no basis option.")
        body["ReportBasis"] = a["basis"]
    if a["columns"]:
        if "SummarizeColumnsBy" not in m.c:
            raise ToolError(f"{r['type']} cannot split columns.")
        body["SummarizeColumnsBy"] = a["columns"]
    for arg, el in ((a["party"], "ReportEntityFilter"), (a["account"], "ReportAccountFilter"), (a["item"], "ReportItemFilter"), (a["class"], "ReportClassFilter")):
        if not arg:
            continue
        if el not in m.c:
            raise ToolError(f"{r['type']} cannot be filtered that way.")
        body[el] = name_ref(arg)
    if r["family"] == "BudgetSummaryReportType":
        body["FiscalYear"] = nz(a["fiscal_year"], int(ctx.today()[:4]))
        body["BudgetCriterion"] = "Accounts"
    return ctx.qb.report(r["message"], body)


@tool(
    "qb_report",
    "Run a QuickBooks report",
    "Run a QuickBooks report (profit and loss, balance sheet, aging, sales by customer or item, inventory, sales tax, "
    "general ledger and more) for a period, and get its rows. Rows are indented by level; the last column is usually the total. "
    "QuickBooks computes these figures itself, so prefer a report over adding up search results.",
    {
        "report": Arg("str", f"Report name: {', '.join(ALIASES)}; or any QuickBooks report type name", required=True),
        "period": Arg("enum", "A QuickBooks period such as LastMonth or ThisFiscalYearToDate", choices=tuple(enums()["DateMacro"])),
        "from": date("Start date (instead of period)"),
        "to": date("End date, or the as-of date for balance sheets and aging"),
        "basis": Arg("enum", "Accounting basis; the company's preference when left out", choices=("Accrual", "Cash")),
        "columns": Arg("enum", "Split columns by, e.g. Month, Quarter, Class (summary reports)", choices=tuple(enums()["SummarizeColumnsBy"])),
        "party": Arg("str", "Only this customer, vendor or employee (full name)"),
        "account": Arg("str", "Only this account (full name)"),
        "item": Arg("str", "Only this item (full name)"),
        "class": Arg("str", "Only this class (full name)"),
        "fiscal_year": Arg("int", "Budget reports: fiscal year"),
        "max_rows": Arg("int", default=300, min=10, max=3000),
    },
)
def report(ctx: Ctx, a: dict) -> dict:
    out = render_report(run_report(ctx, a), a["max_rows"])
    if not out["rows"]:
        # An empty report is easy to read as "nothing to worry about"; say what it means.
        budget = resolve_report(a["report"])["family"] == "BudgetSummaryReportType"
        year = f"fiscal year {text(a['fiscal_year'])}" if a.get("fiscal_year") else "that year"
        out["note"] = "QuickBooks returned no rows: nothing matches this period and these filters" + (
            f", or the company file has no budget for {year} (Company > Planning & Budgeting)." if budget else ".")
    return out


# ---------------------------------------------------------------- qb_schema

CODES = {
    "s": "text", "b": "true/false", "d": "date YYYY-MM-DD", "t": "date-time", "a": "amount", "p": "price", "q": "quantity",
    "r": "percent", "i": "whole number", "f": "number",
}


def _field_line(indent: str, name: str, child: str, repeats: int) -> str:
    kind = f": {CODES.get(child, child)}" if len(child) == 1 else ""
    return f"{indent}{name}{' (repeatable)' if repeats else ''}{kind}"


def _tree(ref: str, depth: int, indent: str, out: list[str]) -> None:
    m = model(ref)
    if m is None:
        return
    for name in m.o:
        child, repeats = m.c[name]
        out.append(_field_line(indent, name, child, repeats))
        if len(child) > 1 and depth > 1:
            _tree(child, depth - 1, indent + "  ", out)


@tool(
    "qb_schema",
    "Look up qbXML fields",
    "Show the fields a qbXML request or record takes, in the order QuickBooks requires, with value types. Use it before "
    "qbd raw or qbd draft. Give request (e.g. InvoiceAdd, CustomerMod, BillQueryRq, InvoiceRet) or search (part of a name).",
    {
        "request": Arg("str", "e.g. InvoiceAdd, CustomerQueryRq, ReceivePaymentAdd"),
        "search": Arg("str", "Find request names containing this text"),
        "depth": Arg("int", default=2, min=1, max=4),
    },
)
def schema(ctx: Ctx, a: dict) -> dict:
    if a["search"]:
        q = a["search"].lower()
        return {"matches": [n for n in messages() if n.endswith("Rq") and q in n.lower()][:60]}
    if not a["request"]:
        raise ToolError("Give request or search.")
    name = re.sub(r"(Rq|Rs)\Z", "", a["request"])
    record = re.sub(r"Ret\Z", "", name)
    # InvoiceAdd lives inside InvoiceAddRq; InvoiceRet inside InvoiceQueryRs.
    candidates = [(a["request"], None), (f"{name}Rq", name), (f"{name}Rq", None), (f"{record}QueryRs", name)]
    for msg, inner in candidates:
        if not has_message(msg):
            continue
        m = message_model(msg)
        ref = m.c[inner][0] if inner and inner in m.c else None
        if inner and not ref:
            continue
        out: list[str] = []
        if ref:
            _tree(ref, a["depth"], "  ", out)
        else:
            for n in m.o:
                child, repeats = m.c[n]
                out.append(_field_line("  ", n, child, repeats))
                if len(child) > 1 and a["depth"] > 1:
                    _tree(child, a["depth"] - 1, "    ", out)
        return {"request": inner if inner is not None else msg, "fields_in_order": "\n".join(out)}
    return {"found": False, "note": f"No request called {a['request']}. Try search."}


# ---------------------------------------------------------------- qb_raw


@tool(
    "qb_raw",
    "Run any qbXML query",
    "Run any qbXML query or report request that the other tools do not cover, given as JSON fields (see qbd schema). "
    "Only queries: changes go through qbd draft. Answers are capped at `limit` records.",
    {
        "request": Arg("str", "A query request name, e.g. ItemSitesQueryRq, PriceLevelQueryRq, GeneralDetailReportQueryRq", required=True),
        "body": Arg("dict", 'Fields of the request as JSON, e.g. {"MaxReturned": 5}', default={}),
        "limit": Arg("int", default=50, min=1, max=1000),
        "count": Arg("bool", "Only QuickBooks' count of the matching records (queries that support it)", default=False),
    },
)
def raw(ctx: Ctx, a: dict) -> dict:
    message = a["request"] if a["request"].endswith("Rq") else f"{a['request']}Rq"
    if not has_message(message):
        raise ToolError(f"QuickBooks has no request called {message}. Use qbd schema --search to find it.")
    if not re.search(r"(Query|ReportQuery)Rq\Z", message):
        raise ToolError("qbd raw only runs queries and reports. To add, change or delete records use qbd draft (needs QBD_ALLOW_WRITES).")
    if a["count"]:
        if not supports_count(message):
            raise ToolError(f"QuickBooks cannot count {message} without returning the records.")
        return {"count": int(ctx.qb.request(message, a["body"], {"metaData": "MetaDataOnly"}).attrs.get("retCount", 0))}
    if message.endswith("ReportQueryRq"):
        if message == "GeneralDetailReportQueryRq" and a["body"].get("GeneralDetailReportType") == "AuditTrail":
            raise ToolError(AUDIT_TRAIL_OFF)
        return render_report(ctx.qb.report(message, a["body"]), a["limit"] * 10)
    if "MaxReturned" in message_model(message).c:
        res = ctx.qb.query(message, a["body"], a["limit"])
        return {"count": len(res.rows), "truncated": res.truncated, "rows": res.rows}
    rs = ctx.qb.request(message, a["body"])
    return {"status": rs.status_message, **rs.body}


READ_TOOLS = [status, profile, search, export, count, changes, snapshot, get_one, report, schema, raw]
