"""Merchant workflows: each tool answers one everyday question with a few bounded queries and returns figures
plus the items that need attention. The agent turns them into prose."""
from __future__ import annotations

import itertools
import math
import re
import statistics
import unicodedata
from typing import Any, Callable

from ..errors import QbConnectionError, QbStatusError, ToolError
from ..jsutil import MISSING, finite, get, is_num, money as fmt, num_str, nz, round2, text, to_number, truthy
from ..qb.catalog import amount_of, entities, entity, ref_name
from ..qb.client import rets_of
from ..qb.dates import add_days, add_months_clamped, days_between, month_end, month_start
from ..qbxml.build import LIST_ID
from ..qbxml.parse import Report, ReportRow, col_index
from ..qbxml.schema import message_model
from .args import Arg
from . import pages
from .common import Ctx, company_facts, date, num, sum_of, tool
from .write import item_price

CAP = 5000  # most records any one workflow reads; results say when it was reached


def column(r: Report, title: str, type_: str) -> int:
    """Index of a report column by title, or by its type when QuickBooks leaves the title blank (as for TxnID)."""
    i = col_index(r, title)
    return i if i >= 0 else next((k for k, c in enumerate(r.columns) if c.type == type_), -1)


def _at(values: list, i: int) -> Any:
    return values[i] if 0 <= i < len(values) else MISSING


def message(e: BaseException) -> str:
    return e.message if isinstance(e, (QbConnectionError, QbStatusError)) else str(e)


def _named(ctx: Ctx, base: str, name: str, field: str, more: tuple[str, ...] = ()) -> dict:
    """The customer, vendor or other list record with this full name, or a ToolError naming similar ones."""
    found = ctx.qb.query(f"{base}QueryRq", {"FullName": [name], "IncludeRetElement": [field, *more]}, 1).rows
    if found:
        return found[0]
    word = (name.split() or [name])[0]
    close = ctx.qb.query(f"{base}QueryRq", {"ActiveStatus": "All", "NameFilter": {"MatchCriterion": "Contains", "Name": word}, "IncludeRetElement": [field]}, 10).rows
    names = ", ".join(text(get(v, field)) for v in close)
    raise ToolError(f'No {base.lower()} named "{name}".' + (f' {base}s with "{word}" in their name: {names}.' if names else ""))


def open_items(ctx: Ctx, side: str, party: Any = MISSING) -> list[dict]:
    """Open receivables or payables from QuickBooks' aging detail report, so amounts and totals are QuickBooks' own: in
    home currency, with the payments, credits and journal entries not applied to anything yet. Bill records cannot
    give this: QuickBooks puts the vendor's whole open balance in every bill's OpenAmount.

    Each item: id, type (as QuickBooks names it: Invoice, Payment, Credit Memo, Bill...), ref, party, date, due, and
    open (home currency; payments and credits not applied yet are negative)."""
    r = ctx.qb.report("AgingReportQueryRq", {
        "AgingReportType": "ARAgingDetail" if side == "AR" else "APAgingDetail",
        "ReportDateMacro": "Today",
        **({"ReportEntityFilter": {"FullNameWithChildren": party}} if party else {}),
        "IncludeColumn": ["TxnID", "TxnType", "Date", "RefNumber", "Name", "DueDate", "OpenBalance"],
    })
    at = {
        "id": column(r, r"^txn ?id\Z", "TxnID"), "type": column(r, r"^type\Z", "TxnType"), "date": column(r, r"^date\Z", "Date"),
        "ref": column(r, r"^num\Z", "RefNumber"), "name": column(r, r"^name\Z", "Name"), "due": column(r, r"^due date\Z", "DueDate"),
        "open": column(r, r"^open balance\Z", "OpenBalance"),
    }
    if at["name"] < 0 or at["open"] < 0:
        raise ToolError(f"QuickBooks sent {r.title or 'the aging detail report'} without its Name and Open Balance columns.")

    def cell(row: ReportRow, i: int) -> Any:
        v = _at(row.values, i)
        return text(v) if i >= 0 and v is not None and v != "" else MISSING

    out = []
    for row in r.rows:
        open_ = _at(row.values, at["open"])
        if row.kind == "data" and is_num(open_) and open_ != 0:
            day = nz(cell(row, at["date"]), ctx.today())
            out.append({
                "id": cell(row, at["id"]), "type": nz(cell(row, at["type"]), ""), "ref": cell(row, at["ref"]),
                "party": nz(cell(row, at["name"]), "?"), "date": day, "due": nz(cell(row, at["due"]), day), "open": open_,
            })
    return out


def by_party(items: list[dict]) -> list[dict]:
    """Groups open items by party and uses each party's unapplied credits on its oldest documents first. Per party:
    balance (what QuickBooks shows the party owes; negative when you owe them), docs (invoices, bills and other charges,
    oldest due first, each with `left`: what stays open once credits are used), credits, and credit (payments and
    credits not applied yet, as a positive amount)."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["party"], []).append(it)
    out = []
    for name, xs in groups.items():
        credits = [x for x in xs if x["open"] < 0]
        credit = round2(-sum_of(credits, lambda x: x["open"]))
        unused = credit
        docs = []
        for d in sorted((x for x in xs if x["open"] > 0), key=lambda x: x["due"]):
            used = min(unused, d["open"])
            unused = round2(unused - used)
            docs.append({**d, "left": round2(d["open"] - used)})
        out.append({"name": name, "balance": sum_of(xs, lambda x: x["open"]), "docs": docs, "credits": credits, "credit": credit})
    return out


def account_list(ctx: Ctx) -> list[dict]:
    res = ctx.qb.query("AccountQueryRq", {"ActiveStatus": "ActiveOnly", "IncludeRetElement": ["FullName", "AccountType", "Balance", "CurrencyRef"]}, 3000)
    return [
        {"name": text(get(r, "FullName")), "type": text(get(r, "AccountType")), "balance": num(get(r, "Balance")), "currency": ref_name(get(r, "CurrencyRef"))}
        for r in res.rows
    ]


def cash_from(accts: list[dict]) -> dict:
    bank = [a for a in accts if a["type"] == "Bank"]
    cards = [a for a in accts if a["type"] == "CreditCard"]
    undeposited = [a for a in accts if re.search("undeposited", a["name"], re.I)]
    # In a multicurrency file each account's balance is in that account's own currency.
    mixed = len({a["currency"] for a in bank if a["balance"] != 0}) > 1
    return {
        "bank_total": sum_of(bank, lambda a: a["balance"]),
        "bank_accounts": [
            {"account": a["name"], "balance": a["balance"], **({"currency": a["currency"]} if mixed and truthy(a["currency"]) else {})}
            for a in bank if a["balance"] != 0
        ],
        "undeposited_funds": sum_of(undeposited, lambda a: a["balance"]),
        "credit_cards_owed": sum_of(cards, lambda a: a["balance"]),
        **({"currency_note": "The bank accounts hold different currencies: each balance is in its account's currency, and bank_total adds them as they are."} if mixed else {}),
    }


def last_number(row: ReportRow | None) -> Any:
    """The value in a report row's last numeric column (the total column)."""
    if row is None:
        return MISSING
    return next((v for v in reversed(row.values) if is_num(v)), MISSING)


def find_row(r: Report, label: str) -> ReportRow | None:
    pattern = re.compile(label, re.I)
    return next((x for x in r.rows if x.kind != "text" and pattern.search(x.label.strip())), None)


def profit_and_loss(ctx: Ctx, from_: str, to: str, basis: Any = MISSING) -> Report:
    body: dict[str, Any] = {"GeneralSummaryReportType": "ProfitAndLossStandard", "ReportPeriod": {"FromReportDate": from_, "ToReportDate": to}}
    if basis:
        body["ReportBasis"] = basis
    return ctx.qb.report("GeneralSummaryReportQueryRq", body)


def pl_totals(r: Report) -> dict:
    return {
        "income": nz(last_number(find_row(r, r"^total income\Z")), 0),
        "gross_profit": nz(last_number(find_row(r, r"^gross profit\Z")), None),
        "expenses": nz(last_number(find_row(r, r"^total expenses?\Z")), 0),
        "net_income": nz(last_number(find_row(r, r"^net income\Z")), 0),
    }


def fiscal_year_start(d: str, first_month: int) -> str:
    year = int(d[0:4]) if int(d[5:7]) >= first_month else int(d[0:4]) - 1
    return f"{year}-{first_month:02d}-01"


def pct(now: float, before: float) -> Any:
    return round2((now - before) / abs(before) * 100) if truthy(before) else None


# ---------------------------------------------------------------- qb_daily_brief


@tool(
    "qb_daily_brief",
    "Daily finance brief",
    "Morning summary of the business: cash in the bank, money owed by customers and how late it is, bills due soon, "
    "sales this month against the same days last month, yesterday's transactions and stock below reorder point, "
    "with a list of what needs attention today.",
    {"upcoming_days": Arg("int", "How far ahead to list bills coming due", default=7, min=1, max=60)},
)
def daily_brief(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    facts = company_facts(ctx)
    out: dict[str, Any] = {"as_of": today, "company": facts.name}
    attention: list[str] = []
    errors: list[str] = []

    def step(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            errors.append(f"{name}: {message(e)}")

    def cash() -> None:
        c = out["cash"] = cash_from(account_list(ctx))
        if c["undeposited_funds"] > 0:
            attention.append(f"{fmt(c['undeposited_funds'])} is waiting in Undeposited Funds; make a bank deposit.")

    def receivables() -> None:
        parties = by_party(open_items(ctx, "AR"))
        docs = [d for p in parties for d in p["docs"]]

        def late(lo: float, hi: float = math.inf) -> float:
            return sum_of([d for d in docs if lo <= days_between(d["due"], today) <= hi], lambda d: d["left"])

        top = [
            {
                "customer": p["name"],
                "overdue": sum_of([d for d in p["docs"] if d["due"] < today], lambda d: d["left"]),
                "over_90": sum_of([d for d in p["docs"] if days_between(d["due"], today) > 90], lambda d: d["left"]),
            }
            for p in parties
        ]
        top = sorted((p for p in top if p["overdue"] > 0), key=lambda p: p["overdue"], reverse=True)
        overdue = late(1)
        credit = sum_of(parties, lambda p: p["credit"])
        # Credit beyond a customer's own invoices: current + overdue - credit_balances = total_owed.
        in_credit = sum_of(parties, lambda p: max(0, -p["balance"]))
        out["receivables"] = {
            "total_owed": sum_of(parties, lambda p: p["balance"]), "current": late(-math.inf, 0), "overdue": overdue,
            "days_1_30": late(1, 30), "days_31_60": late(31, 60), "days_61_90": late(61, 90), "over_90": late(91),
            "customers_overdue": len(top), "top_overdue": top[:5],
            **({"unapplied_payments_or_credits": credit} if truthy(credit) else {}),
            **({"credit_balances": in_credit} if truthy(in_credit) else {}),
        }
        if overdue > 0:
            attention.append(f"{fmt(overdue)} is overdue from {len(top)} customer(s); largest: {top[0]['customer']} ({fmt(top[0]['overdue'])}).")
        if late(91) > 0:
            attention.append(f"{fmt(late(91))} is more than 90 days overdue.")
        if credit > 0:
            attention.append(f"{fmt(credit)} in customer payments and credits is not applied to invoices yet.")

    def payables() -> None:
        parties = by_party(open_items(ctx, "AP"))
        docs = [d for p in parties for d in p["docs"] if d["left"] > 0]
        soon = add_days(today, a["upcoming_days"])
        overdue = [d for d in docs if d["due"] < today]
        due = sorted((d for d in docs if today <= d["due"] <= soon), key=lambda d: d["due"])
        credit = sum_of(parties, lambda p: p["credit"])
        out["payables"] = {
            "open_total": sum_of(parties, lambda p: p["balance"]),
            "overdue_total": sum_of(overdue, lambda d: d["left"]),
            "overdue_bills": len(overdue),
            f"due_next_{a['upcoming_days']}_days": sum_of(due, lambda d: d["left"]),
            "coming_due": [
                {"vendor": d["party"], **({} if d["type"] == "Bill" else {"type": d["type"]}), "ref": d["ref"], "due": d["due"], "amount": d["left"]}
                for d in due[:10]
            ],
            **({"unapplied_credits": credit} if truthy(credit) else {}),
        }
        if overdue:
            attention.append(f"{len(overdue)} bill(s) overdue, {fmt(sum_of(overdue, lambda d: d['left']))} in total.")
        if due:
            attention.append(f"{len(due)} bill(s) due in the next {a['upcoming_days']} days, {fmt(sum_of(due, lambda d: d['left']))} in total.")
        if credit > 0:
            attention.append(f"{fmt(credit)} in vendor credits and payments is not applied to bills yet; use it when paying.")

    def sales() -> None:
        now = pl_totals(profit_and_loss(ctx, month_start(today), today))
        before = pl_totals(profit_and_loss(ctx, month_start(today, -1), add_months_clamped(today, -1)))
        out["sales"] = {"month_to_date": now, "same_days_last_month": before, "income_change_pct": pct(now["income"], before["income"])}

    def yesterday() -> None:
        y = add_days(today, -1)
        res = ctx.qb.query(
            "TransactionQueryRq",
            {"TransactionDateRangeFilter": {"FromTxnDate": y, "ToTxnDate": y}, "TransactionDetailLevelFilter": "SummaryOnly", "IncludeRetElement": ["TxnType", "Amount"]},
            2000,
            page_size=500,
        )
        by_type: dict[str, dict] = {}
        for t in res.rows:
            k = by_type.setdefault(text(get(t, "TxnType")), {"count": 0, "amount": 0})
            k["count"] += 1
            k["amount"] = round2(k["amount"] + abs(num(get(t, "Amount"))))
        out["yesterday"] = {"date": y, "transactions": len(res.rows), "by_type": by_type}

    def inventory() -> None:
        res = ctx.qb.query(
            "ItemInventoryQueryRq",
            {"ActiveStatus": "ActiveOnly", "IncludeRetElement": ["FullName", "QuantityOnHand", "QuantityOnOrder", "QuantityOnSalesOrder", "ReorderPoint"]},
            CAP,
            page_size=500,
        )
        items = [
            {
                "item": text(get(r, "FullName")), "on_hand": num(get(r, "QuantityOnHand")), "reorder_point": num(get(r, "ReorderPoint")),
                "available": num(get(r, "QuantityOnHand")) + num(get(r, "QuantityOnOrder")) - num(get(r, "QuantityOnSalesOrder")),
            }
            for r in res.rows
        ]
        low = sorted((i for i in items if i["reorder_point"] > 0 and i["available"] <= i["reorder_point"]), key=lambda i: i["available"] - i["reorder_point"])
        negative = [i for i in items if i["on_hand"] < 0]
        out["inventory"] = {
            "items_checked": len(items), "below_reorder_point": len(low), "examples": low[:8], "negative_on_hand": len(negative),
            **({"note": f"Only the first {CAP} items were checked."} if res.truncated else {}),
        }
        if low:
            attention.append(f"{len(low)} item(s) at or below their reorder point (run qbd reorder for a purchase list).")
        if negative:
            attention.append(f"{len(negative)} item(s) show negative stock on hand; costs will be wrong until fixed (qbd stock-history <item> shows what took one there).")

    step("cash", cash)
    step("receivables", receivables)
    step("payables", payables)
    step("sales", sales)
    step("yesterday", yesterday)
    if facts.uses_inventory:
        step("inventory", inventory)
    return {**out, "attention": attention, **({"errors": errors} if errors else {})}


# ---------------------------------------------------------------- qb_receivables


def stage(days_late: int) -> str:
    if days_late <= 0:
        return "not due yet"
    if days_late <= 15:
        return "friendly reminder"
    if days_late <= 45:
        return "second reminder"
    if days_late <= 90:
        return "phone call; agree a payment date"
    return "final notice; consider holding new orders"


def _is_invoice(d: dict) -> bool:
    return truthy(d["id"]) and re.search("invoice", d["type"], re.I) is not None


@tool(
    "qb_receivables",
    "Who owes money (collections list)",
    "Who owes money, from QuickBooks' A/R aging: customers most overdue first, with contact details, days late, a "
    "suggested reminder step and their open invoices. Payments and credits not applied yet are used on the oldest "
    "invoices first and flagged, to apply before chasing. Use it to plan collections calls and emails. totals.open is "
    "QuickBooks' A/R total: what customers owe less the credit_balances of customers who paid more than they owe, so "
    "totals.overdue, which counts only customers who owe, can be larger.",
    {
        "customer": Arg("str", "Only this customer (full name)"),
        "top": Arg("int", "How many customers to list", default=15, min=1, max=100),
        "invoices_per_customer": Arg("int", default=5, min=0, max=50),
        "open": Arg("bool", "Also draw it as a page and open it in the web browser"),
    },
)
def receivables(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    parties = by_party(open_items(ctx, "AR", a["customer"]))
    customers = []
    for p in parties:
        overdue = [d for d in p["docs"] if d["due"] < today and d["left"] > 0]
        customers.append({
            **p,
            "overdue": sum_of(overdue, lambda d: d["left"]),
            "oldest_days_late": max(days_between(d["due"], today) for d in overdue) if overdue else 0,
        })
    owing = sorted((c for c in customers if c["balance"] > 0), key=lambda c: (c["overdue"], c["balance"]), reverse=True)
    top = owing[:a["top"]]

    # Contact details come through the customers' invoices, as the report only has names. The invoice also gives
    # its balance in the customer's currency, which a reminder must quote.
    def shown(c: dict) -> list[dict]:
        return c["docs"][:a["invoices_per_customer"]]

    ids = list(dict.fromkeys(d["id"] for c in top for d in [*shown(c), *[x for x in c["docs"] if _is_invoice(x)][:1]] if _is_invoice(d)))
    invoices: dict[str, dict] = {}
    contacts: dict[str, dict] = {}
    notes: list[str] = []
    try:
        if ids:
            res = ctx.qb.query("InvoiceQueryRq", {"TxnID": ids, "IncludeRetElement": ["TxnID", "CustomerRef", "BalanceRemaining", "CurrencyRef"]}, len(ids))
            for r in res.rows:
                invoices[text(get(r, "TxnID"))] = r
        list_ids = list(dict.fromkeys(x for x in (get(get(r, "CustomerRef"), "ListID") for r in invoices.values()) if truthy(x)))
        if list_ids:
            res = ctx.qb.query("CustomerQueryRq", {"ListID": list_ids, "IncludeRetElement": ["ListID", "FullName", "Contact", "Phone", "Email", "TermsRef"]}, len(list_ids))
            for r in res.rows:
                contacts[text(get(r, "ListID"))] = r
    except Exception as e:
        notes.append(f"Could not read contact details: {message(e)}")

    def contact_of(c: dict) -> dict:
        for d in c["docs"]:
            id_ = get(get(invoices.get(nz(d["id"], "")), "CustomerRef"), "ListID")
            if truthy(id_) and text(id_) in contacts:
                return contacts[text(id_)]
        return {}

    def in_currency(d: dict) -> dict:
        inv = invoices.get(nz(d["id"], ""))
        own = num(get(inv, "BalanceRemaining"))
        if truthy(get(inv, "CurrencyRef")) and abs(own - d["open"]) > 0.005:
            return {"currency": ref_name(inv["CurrencyRef"]), "balance_in_currency": own}
        return {}

    def customer(c: dict) -> dict:
        k = contact_of(c)
        return {
            "customer": c["name"],
            "contact": get(k, "Contact"), "email": get(k, "Email"), "phone": get(k, "Phone"), "terms": ref_name(get(k, "TermsRef")),
            "open": c["balance"], "overdue": c["overdue"], "oldest_days_late": c["oldest_days_late"], "open_invoices": len(c["docs"]),
            "next_step": stage(c["oldest_days_late"]),
            **({"unapplied_payments_or_credits": c["credit"], "check_first": "Apply this payment or credit before sending a reminder."} if truthy(c["credit"]) else {}),
            "invoices": [
                {
                    **({} if re.search("invoice", d["type"], re.I) else {"type": d["type"]}), "ref": d["ref"], "date": d["date"], "due": d["due"],
                    "days_late": max(0, days_between(d["due"], today)), "balance": d["open"],
                    **({"after_credits": d["left"]} if d["left"] != d["open"] else {}), **in_currency(d), "txn_id": d["id"],
                }
                for d in shown(c)
            ],
        }

    credit = sum_of(customers, lambda c: c["credit"])
    in_credit = [c for c in customers if c["credit"] > 0 and c["balance"] <= 0]
    # Credit beyond a customer's own invoices, as in daily-brief: owed by the customers who owe - credit_balances = open.
    credit_balances = sum_of(customers, lambda c: max(0, -c["balance"]))
    out = {
        "as_of": today,
        "totals": {
            "open": sum_of(customers, lambda c: c["balance"]),
            "overdue": sum_of(customers, lambda c: c["overdue"]),
            "customers_owing": len(owing),
            "customers_overdue": len([c for c in owing if c["overdue"] > 0]),
            **({"unapplied_payments_or_credits": credit} if truthy(credit) else {}),
            **({"credit_balances": credit_balances} if truthy(credit_balances) else {}),
        },
        "customers": [customer(c) for c in top],
        **({"more_customers": len(owing) - a["top"]} if len(owing) > a["top"] else {}),
        **({
            "customers_in_credit": [
                {
                    "customer": c["name"], "open_invoices": sum_of(c["docs"], lambda d: d["open"]), "unapplied_payments_or_credits": c["credit"],
                    "next_step": "Apply the credit to these invoices; nothing to chase." if c["docs"] else "No open invoice to apply it to: keep it for the next invoice or refund it.",
                }
                for c in in_credit[:10]
            ],
        } if in_credit else {}),
        **({"notes": notes} if notes else {}),
    }
    if a["open"]:
        out.update(pages.receivables(ctx, out, [
            {"customer": c["name"], "balance": c["balance"], "overdue": c["overdue"], "oldest": c["oldest_days_late"],
             "next_step": stage(c["oldest_days_late"]), "credit": c["credit"], "docs": c["docs"]} for c in top]))
    return out


# ---------------------------------------------------------------- qb_match_payment

def _cents(x: float) -> int:
    return int(round(x * 100))


@tool(
    "qb_match_payment",
    "Which invoices a payment pays",
    "For a payment from a customer, the open invoices it most likely pays, from QuickBooks' A/R aging: the invoice "
    "numbers named in the remittance text, the one invoice with that open amount, the oldest invoices in order, or "
    "another set of up to five that adds up exactly; otherwise oldest first, with what stays open and the nearest "
    "invoice. Says how sure the match is, lists the alternatives, and gives the fields for qbd draft add "
    "receive_payment.",
    {
        "customer": Arg("str", "The customer (its jobs included)", required=True),
        "amount": Arg("str", "The payment amount, e.g. 1250.00", required=True),
        "memo": Arg("str", "The remittance text or reference that came with the payment"),
        "date": date("The payment date (default: today)"),
    },
)
def match_payment(ctx: Ctx, a: dict) -> dict:
    amount = to_number(a["amount"].replace(",", ""))
    if not finite(amount) or amount <= 0:
        raise ToolError(f'"{a["amount"]}" is not an amount: give a number such as 1250.00.')
    customer = text(get(_named(ctx, "Customer", a["customer"], "FullName"), "FullName"))
    items = open_items(ctx, "AR", customer)
    docs = sorted((x for x in items if x["open"] > 0), key=lambda x: (x["date"], text(nz(x["ref"], ""))))
    credit = round2(-sum_of((x for x in items if x["open"] < 0), lambda x: x["open"]))
    pay = _cents(amount)

    def doc(x: dict, apply: int) -> dict:
        return {**({} if x["type"] == "Invoice" else {"type": x["type"]}), "ref": x["ref"], "date": x["date"], "due": x["due"],
                "open": x["open"], "apply": apply / 100, "txn_id": x["id"]}

    def plan(xs: list[dict], how: str, sure: str) -> dict:
        left, out = pay, []
        for x in xs:
            part = min(left, _cents(x["open"]))
            left -= part
            out.append(doc(x, part))
        return {"how": how, "confidence": sure, "invoices": out, **({"left_over": left / 100} if left else {})}

    matches: list[dict] = []
    words = set(re.findall(r"[a-z0-9][a-z0-9-]*", text(nz(a["memo"], "")).lower()))
    named = [x for x in docs if truthy(x["ref"]) and (text(x["ref"]).lower() in words or re.sub(r"\D", "", text(x["ref"])) in words)]
    if named:
        s = sum(_cents(x["open"]) for x in named)
        matches.append(plan(named, "the invoices named in the memo", "high") if s == pay else
                       plan(named, f"the invoices named in the memo, though they add up to {fmt(s / 100)}", "medium"))
    same = [x for x in docs if _cents(x["open"]) == pay]
    matches += [plan([x], "the one open invoice with this amount" if len(same) == 1 else "an open invoice with this amount",
                     "high" if len(same) == 1 else "medium") for x in same]
    run = 0
    for k, x in enumerate(docs, 1):
        run += _cents(x["open"])
        if run == pay and k > 1:
            matches.append(plan(docs[:k], "the whole open balance" if k == len(docs) else f"the oldest {k} invoices", "high" if k == len(docs) else "medium"))
        if run >= pay:
            break
    if not matches:
        pool = docs[:25]
        for size in range(2, 6):
            found = [c for c in itertools.combinations(pool, size) if sum(_cents(x["open"]) for x in c) == pay]
            matches += [plan(list(c), f"{size} invoices that add up exactly", "medium" if len(found) == 1 else "low") for c in found[:4]]
            if found:
                break
    if not matches:
        matches.append(plan(docs, "oldest first: no set of open invoices adds up to this amount", "low"))
    best = matches[0]
    near = min(docs, key=lambda x: abs(_cents(x["open"]) - pay), default=None)
    applied = [x for x in best["invoices"] if x["apply"]]
    return {
        "customer": customer,
        "amount": round2(amount),
        "open": {"invoices": len(docs), "total": sum_of(docs, lambda x: x["open"])},
        **({"credits": credit, "credits_note": "Open credits or payments not applied yet: Receive Payments can use them too."} if credit else {}),
        "match": best,
        **({"alternatives": matches[1:4]} if len(matches) > 1 else {}),
        **({"nearest": {"ref": near["ref"], "open": near["open"], "difference": round2(amount - near["open"])}}
           if near is not None and best["confidence"] == "low" else {}),
        "draft_fields": {
            "CustomerRef": customer, "TxnDate": a["date"] or ctx.today(), "TotalAmount": round2(amount),
            **({"Memo": a["memo"]} if a["memo"] else {}),
            "AppliedToTxnAdd": [{"TxnID": x["txn_id"], "PaymentAmount": x["apply"]} for x in applied],
        },
        "next_step": "Show the match to the person. To record the payment: qbd draft add receive_payment with draft_fields "
                     "(left_over stays as a credit on the customer).",
    }


# ---------------------------------------------------------------- qb_deposits

FEE_SHARE = 0.05  # a card batch can arrive less the processor's fee; more than 5% off is not taken for one


@tool(
    "qb_deposits",
    "Payments waiting to be deposited",
    "Customer payments and sales receipts waiting in Undeposited Funds, oldest first, by payment method and by day. Give "
    "the amount of a deposit on the bank statement to find which of them make it up: one payment, one day's payments of "
    "one method (card batches arrive that way, sometimes less the processor's fee), everything waiting, or another set "
    "of up to five that adds up exactly. Gives the fields for qbd draft add deposit.",
    {
        "amount": Arg("str", "The deposit amount on the bank statement, e.g. 2875.67"),
        "date": date("The deposit date on the bank statement (default: today)"),
        "account": Arg("str", "The bank account it went to, for the draft fields"),
        "limit": Arg("int", "Most payments listed", default=50, min=1, max=1000),
    },
)
def deposits(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    on = a["date"] or today
    rows = ctx.qb.query("ReceivePaymentToDepositQueryRq", {}, 2000).rows
    method: dict[str, str] = {}
    for kind in ("ReceivePayment", "SalesReceipt"):
        ids = [text(get(r, "TxnID")) for r in rows if get(r, "TxnType") == kind]
        if ids:
            for r in ctx.qb.query(f"{kind}QueryRq", {"TxnID": ids, "IncludeRetElement": ["TxnID", "PaymentMethodRef"]}, len(ids) + 1).rows:
                method[text(get(r, "TxnID"))] = text(nz(ref_name(get(r, "PaymentMethodRef")), "(no method)"))
    pays = sorted(({"date": text(get(r, "TxnDate")), "method": method.get(text(get(r, "TxnID")), "(no method)"),
                    "customer": ref_name(get(r, "CustomerRef")), "ref": get(r, "RefNumber"), "amount": num(get(r, "Amount")),
                    "days": days_between(text(get(r, "TxnDate")), today), "txn_id": get(r, "TxnID"),
                    **({} if get(r, "TxnType") == "ReceivePayment" else {"type": get(r, "TxnType")})} for r in rows),
                  key=lambda x: (x["date"], text(nz(x["ref"], ""))))

    def group(key: Callable[[dict], Any]) -> dict:
        out: dict = {}
        for x in pays:
            out.setdefault(key(x), []).append(x)
        return out

    by_method = group(lambda x: x["method"])
    batches = group(lambda x: (x["date"], x["method"]))
    out: dict[str, Any] = {
        "as_of": today,
        "waiting": {"payments": len(pays), "total": sum_of(pays, lambda x: x["amount"]), **({"oldest": pays[0]["date"]} if pays else {}),
                    "older_than_7_days": sum(x["days"] > 7 for x in pays)},
        "by_method": [{"method": m, "payments": len(xs), "total": sum_of(xs, lambda x: x["amount"])} for m, xs in by_method.items()],
        "by_day": [{"date": d, "method": m, "payments": len(xs), "total": sum_of(xs, lambda x: x["amount"])}
                   for (d, m), xs in sorted(batches.items(), reverse=True)][:20],
        "payments": pays[: a["limit"]],
        **({"more_payments": len(pays) - a["limit"]} if len(pays) > a["limit"] else {}),
    }
    if not truthy(a["amount"]):
        return out
    amount = to_number(a["amount"].replace(",", ""))
    if not finite(amount) or amount <= 0:
        raise ToolError(f'"{a["amount"]}" is not an amount: give a number such as 2875.67.')
    want = _cents(amount)
    pool = [x for x in pays if x["date"] <= on]

    def plan(xs: list[dict], how: str, sure: str) -> dict:
        got = sum(_cents(x["amount"]) for x in xs)
        return {"how": how, "confidence": sure, "payments": [{k: x[k] for k in ("date", "method", "customer", "ref", "amount", "txn_id")} for x in xs],
                "total": got / 100, **({"fee": (got - want) / 100} if got != want else {})}

    matches: list[dict] = []
    same = [x for x in pool if _cents(x["amount"]) == want]
    matches += [plan([x], "the one payment with this amount" if len(same) == 1 else "a payment with this amount", "high" if len(same) == 1 else "medium") for x in same]
    exact = [(k, xs) for k, xs in batches.items() if k[0] <= on and sum(_cents(x["amount"]) for x in xs) == want and len(xs) > 1]
    matches += [plan(xs, f"the {m} payments of {d}", "high" if len(exact) == 1 else "medium") for (d, m), xs in exact]
    days = [(d, xs) for d, xs in group(lambda x: x["date"]).items() if d <= on and len({x["method"] for x in xs}) > 1 and sum(_cents(x["amount"]) for x in xs) == want]
    matches += [plan(xs, f"all the payments of {d}", "medium") for d, xs in days]
    if len(pool) > 1 and sum(_cents(x["amount"]) for x in pool) == want:
        matches.append(plan(pool, "everything waiting", "high"))
    if not matches:
        recent = pool[-25:]
        for size in range(2, 6):
            found = [c for c in itertools.combinations(recent, size) if sum(_cents(x["amount"]) for x in c) == want]
            matches += [plan(list(c), f"{size} payments that add up exactly", "medium" if len(found) == 1 else "low") for c in found[:4]]
            if found:
                break
    if not matches:
        fee = sorted(((sum(_cents(x["amount"]) for x in xs) - want, k, xs) for k, xs in batches.items()
                      if k[0] <= on and 0 < sum(_cents(x["amount"]) for x in xs) - want <= FEE_SHARE * sum(_cents(x["amount"]) for x in xs)),
                     key=lambda t: t[0])
        matches += [plan(xs, f"the {m} payments of {d}, less a fee of {fmt(gap / 100)}", "medium" if len(fee) == 1 else "low") for gap, (d, m), xs in fee[:3]]
    out["deposit"] = {"amount": round2(amount), "date": on}
    if not matches:
        near = min(pays, key=lambda x: abs(_cents(x["amount"]) - want), default=None)
        out["match"] = {"how": "no payment, day or set of up to five adds up to this amount", "confidence": "none",
                        **({"nearest": {"ref": near["ref"], "customer": near["customer"], "amount": near["amount"]}} if near else {})}
        out["next_step"] = "Check the deposit slip or the processor's report: the deposit may hold a payment not entered yet, or one already deposited."
        return out
    best = matches[0]
    out["match"] = best
    if len(matches) > 1:
        out["alternatives"] = matches[1:4]
    out["draft_fields"] = {**({"DepositToAccountRef": a["account"]} if a["account"] else {}), "TxnDate": on,
                           "DepositLineAdd": [{"PaymentTxnID": x["txn_id"]} for x in best["payments"]]}
    out["next_step"] = ("Show the match to the person. To record it: qbd draft add deposit with draft_fields"
                        + ("" if a["account"] else ", adding DepositToAccountRef (the bank account)")
                        + (f", plus a line {{\"AccountRef\": \"<the merchant fees account>\", \"Amount\": -{best['fee']:.2f}}} for the fee" if "fee" in best else "") + ".")
    return out


# ---------------------------------------------------------------- qb_trace

TXN_KEYS = tuple(e.key for e in entities() if e.kind == "txn")
TRACE_CAP = 40  # documents followed at most


def _fetch(ctx: Ctx, type_: str, id_: str) -> dict | None:
    m = message_model(f"{type_}QueryRq")
    if m is None or "TxnID" not in m.c:
        return None
    body = {"TxnID": [id_], **({"IncludeLineItems": True} if type_ == "Deposit" else {}), **({"IncludeLinkedTxns": True} if "IncludeLinkedTxns" in m.c else {})}
    try:
        rows = ctx.qb.query(f"{type_}QueryRq", body, 1).rows
    except QbStatusError:
        return None
    return rows[0] if rows else None


def _state(type_: str, r: dict) -> tuple[str, str]:
    """A document's state, and what is still to do about it ("" when nothing)."""
    if type_ == "Invoice":
        left = num(get(r, "BalanceRemaining"))
        return ("paid", "") if get(r, "IsPaid") is True or left == 0 else (f"{fmt(left)} open", f"{fmt(left)} still to collect")
    if type_ == "Bill":
        return ("paid", "") if get(r, "IsPaid") is True else ("not paid", "still to pay")
    if type_ == "SalesOrder":
        return (("fully invoiced", "") if get(r, "IsFullyInvoiced") is True else ("closed", "") if get(r, "IsManuallyClosed") is True
                else ("open", "not fully invoiced"))
    if type_ == "PurchaseOrder":
        return (("fully received", "") if get(r, "IsFullyReceived") is True else ("closed", "") if get(r, "IsManuallyClosed") is True
                else ("open", "not fully received"))
    if type_ == "CreditMemo":
        left = num(get(r, "CreditRemaining"))
        return (f"{fmt(left)} credit left", f"{fmt(left)} credit not used yet") if left > 0 else ("used", "")
    if type_ in ("ReceivePayment", "BillPaymentCheck", "BillPaymentCreditCard"):
        unused = num(get(r, "UnusedPayment"))
        return (f"{fmt(unused)} not applied", f"{fmt(unused)} not applied to anything") if unused > 0 else ("applied", "")
    return "", ""


@tool(
    "qb_trace",
    "Follow a document through its chain",
    "Start from one transaction, by its number or TxnID, and follow QuickBooks' links: estimate or sales order to "
    "invoices, payments and the bank deposit; purchase order to item receipts, bills and bill payments; and back. Each "
    "document has its state (open, paid, fully invoiced, waiting in Undeposited Funds...), and loose_ends lists what is "
    "still to do. Use it for \"where is this order?\" or \"was this invoice paid and banked?\".",
    {
        "ref": Arg("str", "The transaction's number (RefNumber), or its TxnID", required=True),
        "type": Arg("enum", "The record type, when several transactions have this number", choices=TXN_KEYS),
    },
)
def trace(ctx: Ctx, a: dict) -> dict:
    ref = a["ref"].strip()
    found = ctx.qb.query("TransactionQueryRq", {"TxnID": [ref]} if LIST_ID.match(ref) else {"RefNumber": [ref]}, 20).rows
    if a["type"]:
        found = [t for t in found if get(t, "TxnType") == entity(a["type"]).base]
    if not found:
        raise ToolError(f'No transaction numbered "{ref}"' + (f" of type {a['type']}." if a["type"] else "."))
    if len(found) > 1:
        return {"ref": ref, "several": [{"type": get(t, "TxnType"), "date": get(t, "TxnDate"), "party": ref_name(get(t, "EntityRef")),
                                         "amount": get(t, "Amount"), "txn_id": get(t, "TxnID")} for t in found],
                "next_step": "Several transactions have this number: run qbd trace again with type, or with the TxnID."}
    waiting: list[str] | None = None
    seen: dict[str, dict] = {}
    queue: list[tuple[str, str, dict | None]] = [(text(get(found[0], "TxnType")), text(get(found[0], "TxnID")), None)]
    loose: list[str] = []
    while queue and len(seen) < TRACE_CAP:
        type_, id_, parent = queue.pop(0)
        if id_ in seen:
            continue
        r = _fetch(ctx, type_, id_)
        if r is None:
            seen[id_] = {"type": type_, "txn_id": id_, "state": "not readable through the SDK"}
            continue
        state, todo = _state(type_, r)
        node = {
            "type": type_, "ref": get(r, "RefNumber"), "date": get(r, "TxnDate"),
            "party": ref_name(nz(get(r, "CustomerRef"), get(r, "VendorRef"), get(r, "PayeeEntityRef"), get(r, "EntityRef"))),
            "amount": amount_of(r),
            **({"via": f"{parent['type']} {text(nz(parent['ref'], parent['txn_id']))}"} if parent else {}),
            "txn_id": id_,
        }
        seen[id_] = node
        links = [(text(get(x, "TxnType")), text(get(x, "TxnID"))) for k in ("LinkedTxn", "AppliedToTxnRet", "DepositLineRet")
                 for x in _many(get(r, k)) if truthy(get(x, "TxnID"))]
        if type_ in ("ReceivePayment", "SalesReceipt"):
            if waiting is None:
                waiting = [text(get(x, "TxnID")) for x in ctx.qb.query("ReceivePaymentToDepositQueryRq", {}, 2000).rows]
            account = ref_name(get(r, "DepositToAccountRef"))
            bank = isinstance(account, str) and any(text(get(x, "AccountType")) == "Bank" for x in ctx.qb.query(
                "AccountQueryRq", {"FullName": [account], "IncludeRetElement": ["AccountType"]}, 1).rows)
            if id_ in waiting:
                state, todo = "waiting in Undeposited Funds", "not deposited yet"
            elif bank:
                state = f"deposited to {account}"
            else:
                day = text(get(r, "TxnDate"))
                deposits = ctx.qb.query("DepositQueryRq", {"TxnDateRangeFilter": {"FromTxnDate": day, "ToTxnDate": add_days(day, 90)}, "IncludeLineItems": True}, 300).rows
                dep = next((d for d in deposits if any(get(x, "TxnID") == id_ for x in _many(get(d, "DepositLineRet")))), None)
                state = "deposited" if dep else "not waiting to be deposited, but no deposit in the 90 days after lists it"
                if dep:
                    links.append(("Deposit", text(get(dep, "TxnID"))))
        # From a payment, do not wander into the other invoices it paid (or a bill payment into other bills): count them.
        siblings = [x for x in links if parent and x[0] == parent["type"] and x[1] != parent["txn_id"]]
        node.update({**({"state": state} if state else {}), **({"also_linked": f"{len(siblings)} other {siblings[0][0]}(s)"} if siblings else {})})
        if todo:
            loose.append(f"{type_} {text(nz(node['ref'], id_))}: {todo}")
        queue += [(t, i, node) for t, i in links if i not in seen and (t, i) not in siblings]
    chain = list(seen.values())
    return {
        "start": chain[0],
        "chain": chain[1:],
        "loose_ends": loose,
        **({"note": f"Stopped after {TRACE_CAP} documents."} if queue else {}),
    }


# ---------------------------------------------------------------- qb_payables


@tool(
    "qb_payables",
    "Bills to pay",
    "Bills to pay, from QuickBooks' A/P aging: what is overdue, what falls due in the next days, totals per vendor "
    "and the cash needed. Vendor credits and payments not applied yet are used on each vendor's oldest bills first "
    "and flagged, to apply when paying. Use it to plan a payment run.",
    {
        "vendor": Arg("str", "Only this vendor"),
        "days_ahead": Arg("int", default=14, min=1, max=90),
        "top": Arg("int", "How many vendors to list", default=20, min=1, max=100),
    },
)
def payables(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    end = add_days(today, a["days_ahead"])
    parties = by_party(open_items(ctx, "AP", a["vendor"]))

    def is_overdue(d: dict) -> bool:
        return d["due"] < today

    def is_soon(d: dict) -> bool:
        return today <= d["due"] <= end

    to_pay = [d for p in parties for d in p["docs"] if d["left"] > 0 and (is_overdue(d) or is_soon(d))]
    vendors = [
        {
            "vendor": p["name"],
            "open": p["balance"],
            "overdue": sum_of([d for d in p["docs"] if is_overdue(d)], lambda d: d["left"]),
            "due_soon": sum_of([d for d in p["docs"] if is_soon(d)], lambda d: d["left"]),
            "next_due": next((d["due"] for d in p["docs"] if d["left"] > 0), MISSING),
            "bills": len(p["docs"]),
            **({"unapplied_credits": p["credit"], "check_first": "Apply these credits in Pay Bills before paying."} if truthy(p["credit"]) else {}),
        }
        for p in parties
    ]
    vendors = sorted((v for v in vendors if v["bills"] or truthy(v["open"])), key=lambda v: (v["overdue"] + v["due_soon"], v["open"]), reverse=True)
    credit = sum_of(parties, lambda p: p["credit"])
    return {
        "as_of": today,
        "totals": {
            "open": sum_of(parties, lambda p: p["balance"]),
            "overdue": sum_of(vendors, lambda v: v["overdue"]),
            f"due_next_{a['days_ahead']}_days": sum_of(vendors, lambda v: v["due_soon"]),
            "cash_needed_now_and_soon": sum_of(to_pay, lambda d: d["left"]),
            **({"unapplied_credits": credit} if truthy(credit) else {}),
        },
        "vendors": vendors[:a["top"]],
        "bills_to_pay": [
            {
                "vendor": d["party"], **({} if d["type"] == "Bill" else {"type": d["type"]}), "ref": d["ref"], "bill_date": d["date"], "due": d["due"],
                "days_late": max(0, days_between(d["due"], today)), "amount": d["left"], **({"open_on_bill": d["open"]} if d["left"] != d["open"] else {}),
                "txn_id": d["id"],
            }
            for d in sorted(to_pay, key=lambda d: d["due"])[:40]
        ],
        **({"more_bills": len(to_pay) - 40} if len(to_pay) > 40 else {}),
    }


# ---------------------------------------------------------------- qb_coding

CODED = (("Bill", "bills"), ("Check", "checks"), ("CreditCardCharge", "card_charges"))
HOLDING = re.compile(r"uncategori[sz]ed|ask my accountant|suspense", re.I)  # accounts for "sort it out later"
SHOWN = 50  # accounts, items and transactions to check, each


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    return round2((s[(len(s) - 1) // 2] + s[len(s) // 2]) / 2)


@tool(
    "qb_coding",
    "How a vendor's spending is coded",
    "How a vendor's bills, checks and credit card charges were coded: the accounts and items their lines went to, with "
    "classes, how many transactions, totals, the typical amount, item costs, recent memos and the latest one; the "
    "accounts QuickBooks pre-fills for the vendor; and transactions coded to a holding account such as Uncategorized "
    "Expenses, or unlike all the others. Use it before drafting a bill or expense, as the evidence for the account to "
    "choose, or to check a new bill's prices against past costs.",
    {
        "vendor": Arg("str", "The vendor", required=True),
        "from": date("Oldest transaction date to read (default: a year ago)"),
        "limit": Arg("int", "Most transactions of each kind to read", default=500, min=1, max=5000),
    },
)
def coding(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    start = a["from"] or add_days(today, -365)
    found = _named(ctx, "Vendor", a["vendor"], "Name", ("PrefillAccountRef",))
    vendor = text(get(found, "Name"))
    prefill = [n for n in (ref_name(r) for r in _many(get(found, "PrefillAccountRef"))) if isinstance(n, str)]

    read: dict[str, int] = {}
    capped: list[str] = []
    txns: list[dict] = []
    for base, key in CODED:
        res = ctx.qb.query(f"{base}QueryRq", {
            "TxnDateRangeFilter": {"FromTxnDate": start, "ToTxnDate": today}, "EntityFilter": {"FullName": [vendor]}, "IncludeLineItems": True,
            "IncludeRetElement": ["TxnID", "TxnDate", "RefNumber", "Memo", "ExpenseLineRet", "ItemLineRet", "ItemGroupLineRet"],
        }, a["limit"])
        read[key] = len(res.rows)
        if res.truncated:
            capped.append(key)
        for t in res.rows:
            lines = [*(("account", x) for x in _many(get(t, "ExpenseLineRet"))), *(("item", x) for x in _many(get(t, "ItemLineRet"))),
                     *(("item", x) for g in _many(get(t, "ItemGroupLineRet")) for x in _many(get(g, "ItemLineRet")))]
            txns.append({"type": base, "id": get(t, "TxnID"), "date": text(get(t, "TxnDate")), "ref": get(t, "RefNumber"), "memo": get(t, "Memo"), "lines": lines})
    txns.sort(key=lambda t: t["date"])

    # One row per account or item and class, filled oldest first so the last transaction and cost are the newest.
    rows: dict[tuple, dict] = {}
    for t in txns:
        for kind, line in t["lines"]:
            name = ref_name(get(line, "AccountRef" if kind == "account" else "ItemRef"))
            if not isinstance(name, str):
                continue
            cls = ref_name(get(line, "ClassRef"))
            r = rows.setdefault((kind, name.lower(), text(nz(cls, "")).lower()),
                                {"kind": kind, "name": name, "class": cls, "txns": [], "amounts": [], "quantity": 0, "costs": [], "memos": [], "jobs": []})
            if not r["txns"] or r["txns"][-1] is not t:
                r["txns"].append(t)
            r["amounts"].append(num(get(line, "Amount")))
            if kind == "item":
                r["quantity"] += num(get(line, "Quantity"))
                if is_num(get(line, "Cost")):
                    r["costs"].append(get(line, "Cost"))
            memo, job = nz(get(line, "Memo"), t["memo"]), ref_name(get(line, "CustomerRef"))
            if truthy(memo):
                r["memos"].append(text(memo))
            if isinstance(job, str):
                r["jobs"].append(job)
    ordered = sorted(rows.values(), key=lambda r: (len(r["txns"]), sum_of(r["amounts"], abs)), reverse=True)

    def show(r: dict) -> dict:
        last = r["txns"][-1]
        memos = list(dict.fromkeys(reversed(r["memos"])))[:3]
        return {
            r["kind"]: r["name"], **({"class": r["class"]} if isinstance(r["class"], str) else {}),
            "transactions": len(r["txns"]), "share": round2(len(r["txns"]) / len(txns)), "total": sum_of(r["amounts"], lambda x: x),
            **({"typical": _median(r["amounts"])} if r["kind"] == "account" else {}),
            **({"quantity": round2(r["quantity"]), "last_cost": r["costs"][-1], "cost_range": [min(r["costs"]), max(r["costs"])]} if r["costs"] else {}),
            "last": {"date": last["date"], "type": last["type"], **({"ref": last["ref"]} if truthy(last["ref"]) else {}), "id": last["id"]},
            **({"memos": memos} if memos else {}),
            **({"billed_to": list(dict.fromkeys(r["jobs"]))[:3]} if r["jobs"] else {}),
        }

    # The account most transactions went to, whatever the class.
    by_account: dict[str, set] = {}
    for r in ordered:
        if r["kind"] == "account":
            by_account.setdefault(r["name"], set()).update(id(t) for t in r["txns"])
    coded = sum(1 for t in txns if any(k == "account" for k, _ in t["lines"]))
    usual, usual_txns = max(by_account.items(), key=lambda kv: len(kv[1]), default=(MISSING, set()))
    check = []
    for t in reversed(txns):
        for kind, line in t["lines"]:
            name = ref_name(get(line, "AccountRef")) if kind == "account" else MISSING
            if not isinstance(name, str):
                continue
            if HOLDING.search(name):
                why = f"coded to {name}, a holding account" + (f"; this vendor's spending usually goes to {usual}" if name != usual else "")
            elif name != usual and len(by_account[name]) == 1 and len(usual_txns) >= 3 and len(usual_txns) >= 0.6 * coded:
                why = f"the only transaction coded to {name}; the others mostly went to {usual}"
            else:
                continue
            check.append({"date": t["date"], "type": t["type"], **({"ref": t["ref"]} if truthy(t["ref"]) else {}), "id": t["id"],
                          "account": name, "amount": num(get(line, "Amount")), "why": why})
    notes = []
    if prefill and usual is not MISSING and usual.lower() not in (p.lower() for p in prefill):
        notes.append(f"QuickBooks pre-fills {', '.join(prefill)} for this vendor, but most of its transactions went to {usual}: "
                     "check the vendor's Account Prefill (Edit Vendor > Account Settings).")
    if capped:
        notes.append(f"Only the first {a['limit']} {' and '.join(k.replace('_', ' ') for k in capped)} since {start} were read: a later from date reads fewer.")
    if not txns:
        notes.append(f"No bills, checks or credit card charges for this vendor since {start}.")
    accounts = [show(r) for r in ordered if r["kind"] == "account"]
    items = [show(r) for r in ordered if r["kind"] == "item"]
    return {
        "vendor": vendor,
        "from": start,
        "to": today,
        "read": read,
        **({"prefill_accounts": prefill} if prefill else {}),
        **({"usually": {"account": usual, "transactions": len(usual_txns), "of": coded}} if usual is not MISSING else {}),
        "accounts": accounts[:SHOWN],
        **({"more_accounts": len(accounts) - SHOWN} if len(accounts) > SHOWN else {}),
        "items": items[:SHOWN],
        **({"more_items": len(items) - SHOWN} if len(items) > SHOWN else {}),
        **({"check": check[:SHOWN]} if check else {}),
        **({"more_check": len(check) - SHOWN} if len(check) > SHOWN else {}),
        **({"notes": notes} if notes else {}),
    }


# ---------------------------------------------------------------- qb_cash_position


@tool(
    "qb_cash_position",
    "Cash position and short-term forecast",
    "Cash in the bank now and a week-by-week estimate for the coming weeks from invoice and bill due dates, with the "
    "lowest point. An estimate: it does not know payroll, rent or other costs that are not entered as bills.",
    {
        "days_ahead": Arg("int", default=30, min=7, max=90),
        "count_overdue_receivables": Arg("bool", "Assume overdue invoices are paid in the first week", default=False),
        "open": Arg("bool", "Also draw it as a page and open it in the web browser"),
    },
)
def cash_position(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    cash = cash_from(account_list(ctx))
    owed = [d for p in by_party(open_items(ctx, "AR")) for d in p["docs"] if d["left"] > 0]
    bills = [d for p in by_party(open_items(ctx, "AP")) for d in p["docs"] if d["left"] > 0]
    start = round2(cash["bank_total"] + cash["undeposited_funds"])
    overdue_in = [d for d in owed if d["due"] < today]

    # The weeks, the lowest point and the invoices and bills behind each week (the page shows them).
    def forecast(count_overdue: bool) -> tuple[list[dict], dict, list[dict]]:
        running = start
        lowest = {"amount": start, "week_end": today}
        table, docs = [], []
        for w in range(math.ceil(a["days_ahead"] / 7)):
            from_ = add_days(today, w * 7)
            to = add_days(from_, 6)
            ins = [d for d in owed if from_ <= d["due"] <= to]
            outs = [d for d in bills if from_ <= d["due"] <= to or (w == 0 and d["due"] < today)]
            money_in = sum_of(ins, lambda d: d["left"]) + (sum_of(overdue_in, lambda d: d["left"]) if w == 0 and count_overdue else 0)
            money_out = sum_of(outs, lambda d: d["left"])
            running = round2(running + money_in - money_out)
            if running < lowest["amount"]:
                lowest = {"amount": running, "week_end": to}
            table.append({"week_start": from_, "week_end": to, "money_in": round2(money_in), "money_out": money_out, "closing_estimate": running})
            docs.append({"in": ins, "out": outs})
        return table, lowest, docs

    table, lowest, _ = forecast(a["count_overdue_receivables"])
    out = {
        "as_of": today,
        "starting_cash": {"bank": cash["bank_total"], "undeposited_funds": cash["undeposited_funds"], "total": start},
        "credit_cards_owed": cash["credit_cards_owed"],
        "weeks": table,
        "lowest_point": lowest,
        "overdue_receivables": {"amount": sum_of(overdue_in, lambda d: d["left"]), "documents": len(overdue_in), "counted": a["count_overdue_receivables"]},
        "assumptions": [
            "Customers pay on the due date; overdue invoices are left out unless count_overdue_receivables is true.",
            "Overdue bills are paid in the first week; other bills on their due date.",
            "Payments and credits not applied yet are used on the oldest invoices and bills first.",
            "Costs not entered as bills (payroll, card spending, loan payments) are not included.",
            *(["Bank balances are in each account's currency and invoices and bills in the home currency; the forecast adds them without converting."] if "currency_note" in cash else []),
        ],
    }
    if a["open"]:
        out.update(pages.cash(ctx, out, forecast(False), forecast(True), overdue_in))
    return out


# ---------------------------------------------------------------- qb_inventory_reorder


@tool(
    "qb_inventory_reorder",
    "What to reorder",
    "Inventory items that need reordering, from reorder points, stock on order, open sales orders and recent sales "
    "speed, grouped by preferred vendor with a suggested quantity and cost. Ready to turn into purchase orders.",
    {
        "vendor": Arg("str", "Only items whose preferred vendor is this"),
        "lookback_days": Arg("int", "Sales history used for sales speed", default=90, min=14, max=365),
        "cover_days": Arg("int", "Days of sales the stock should cover", default=30, min=7, max=180),
        "limit": Arg("int", default=60, min=1, max=500),
    },
)
def inventory_reorder(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    res = ctx.qb.query(
        "ItemInventoryQueryRq",
        {
            "ActiveStatus": "ActiveOnly",
            "IncludeRetElement": ["ListID", "FullName", "Name", "QuantityOnHand", "QuantityOnOrder", "QuantityOnSalesOrder", "ReorderPoint", "Max", "PurchaseCost", "PrefVendorRef"],
        },
        CAP * 2,
        page_size=500,
    )
    sold: dict[str, float] = {}
    history = True
    try:
        r = ctx.qb.report("GeneralSummaryReportQueryRq", {
            "GeneralSummaryReportType": "SalesByItemSummary",
            "ReportPeriod": {"FromReportDate": add_days(today, -a["lookback_days"]), "ToReportDate": today},
        })
        # QuickBooks puts the period above each column title, so the title reads e.g. "Apr - Jun 25 Qty".
        q = col_index(r, r"(^|\s)qty\Z")
        if q < 0:
            history = False
        else:
            for row in r.rows:
                if row.kind == "data" and is_num(_at(row.values, q)):
                    sold[row.label] = row.values[q]
    except Exception:
        history = False
    lines = []
    for r in res.rows:
        name = text(get(r, "FullName"))
        short = text(nz(get(r, "Name"), name.split(":")[-1]))
        on_hand = num(get(r, "QuantityOnHand"))
        available = round2(on_hand + num(get(r, "QuantityOnOrder")) - num(get(r, "QuantityOnSalesOrder")))
        per_day = nz(sold.get(name, MISSING), sold.get(short, MISSING), 0) / a["lookback_days"]
        rp = num(get(r, "ReorderPoint"))
        max_ = num(get(r, "Max"))
        cover = max(0, math.floor(available / per_day)) if per_day > 0 else None
        needs = (rp > 0 and available <= rp) or (cover is not None and cover < a["cover_days"])
        target = max_ if max_ > 0 else rp + math.ceil(per_day * a["cover_days"])
        suggested = max(0, math.ceil(target - available))
        lines.append({
            "item": name, "vendor": nz(ref_name(get(r, "PrefVendorRef")), "(no preferred vendor)"), "on_hand": on_hand,
            "on_order": num(get(r, "QuantityOnOrder")), "on_sales_orders": num(get(r, "QuantityOnSalesOrder")), "available": available,
            "reorder_point": rp if truthy(rp) else None, "max": max_ if truthy(max_) else None, "sold_per_day": round2(per_day),
            "days_of_cover": cover, "suggested_qty": suggested, "unit_cost": num(get(r, "PurchaseCost")),
            "est_cost": round2(suggested * num(get(r, "PurchaseCost"))), "needs": needs,
        })
    vendor = a["vendor"].lower() if a["vendor"] else None
    want = [x for x in lines if x["needs"] and x["suggested_qty"] > 0 and (not vendor or x["vendor"].lower() == vendor)]
    want.sort(key=lambda x: (nz(x["days_of_cover"], 1e9), x["available"] - nz(x["reorder_point"], 0)))
    picked = want[:a["limit"]]
    by_vendor: dict[str, list[dict]] = {}
    for x in picked:
        by_vendor.setdefault(x["vendor"], []).append(x)
    return {
        "as_of": today,
        "items_checked": len(lines),
        "sales_history": {"from": add_days(today, -a["lookback_days"]), "to": today} if history else "not available; using reorder points only",
        "reorder": [
            {"vendor": v, "est_total": sum_of(items, lambda i: i["est_cost"]), "items": [{k: x for k, x in i.items() if k not in ("needs", "vendor")} for i in items]}
            for v, items in by_vendor.items()
        ],
        "negative_on_hand": [{"item": x["item"], "on_hand": x["on_hand"]} for x in lines if x["on_hand"] < 0],
        **({"more_items": len(want) - len(picked)} if len(want) > len(picked) else {}),
        **({"note": f"Only the first {CAP * 2} items were checked."} if res.truncated else {}),
        "next_step": "To order, use qbd draft for one purchase_order per vendor with these items and quantities, then show it for approval."
        if ctx.config.allow_writes
        else "Writes are off; the owner can place these orders in QuickBooks, or enable writes to let the agent draft purchase orders.",
    }


def _many(v: Any) -> list:
    return v if isinstance(v, list) else [] if v is MISSING or v is None else [v]


def inventory_sites(ctx: Ctx) -> list[dict]:
    """Inventory sites (Advanced Inventory): name, and whether QuickBooks uses it when a line names none."""
    rows = ctx.qb.query("InventorySiteQueryRq", {"ActiveStatus": "All", "IncludeRetElement": ["Name", "IsDefaultSite"]}, 500).rows
    return [{"name": text(get(r, "Name")), "default": get(r, "IsDefaultSite") is True} for r in rows]


def stock_rows(ctx: Ctx, names: list[str], site: Any, read: int) -> tuple[list[dict], bool]:
    """Stock per item, and per site when inventory sites are on. Available is on hand minus open sales orders and parts
    reserved for pending builds, as QuickBooks counts it. Returns the rows and whether more were left unread."""
    rows: list[dict] = []
    truncated = False
    if company_facts(ctx).uses_sites:
        fil = {k: {"FullName": v} for k, v in (("ItemFilter", names), ("SiteFilter", [site] if site is not MISSING else [])) if v}
        res = ctx.qb.query("ItemSitesQueryRq", {"ItemSiteFilter": fil} if fil else {}, read, page_size=500)
        truncated = res.truncated
        for r in res.rows:
            item = nz(get(r, "ItemInventoryRef"), get(r, "ItemInventoryAssemblyRef"))
            rows.append({
                "item": ref_name(item), "site": ref_name(get(r, "InventorySiteRef")), "on_hand": num(get(r, "QuantityOnHand")),
                "on_sales_orders": num(get(r, "QuantityOnSalesOrders")), "reserved_for_builds": num(get(r, "QuantityRequiredByPendingBuildTxns")),
                "on_purchase_orders": num(get(r, "QuantityOnPurchaseOrders")), "to_be_built": num(get(r, "QuantityToBeBuiltByPendingBuildTxns")),
                "pending_transfers": num(get(r, "QuantityOnPendingTransfers")),
                "reorder_point": num(nz(get(r, "AssemblyBuildPoint"), get(r, "ReorderLevel"))),
            })
    else:
        for message, point in (("ItemInventoryQueryRq", "ReorderPoint"), ("ItemInventoryAssemblyQueryRq", "BuildPoint")):
            body = {"FullName": names} if names else {"ActiveStatus": "ActiveOnly"}
            try:
                res = ctx.qb.query(message, {**body, "IncludeRetElement": ["FullName", "QuantityOnHand", "QuantityOnOrder", "QuantityOnSalesOrder", point]}, read, page_size=500)
            except QbStatusError:
                if message == "ItemInventoryQueryRq":
                    raise
                continue  # no assemblies in this edition
            truncated |= res.truncated
            rows.extend({
                "item": text(get(r, "FullName")), "on_hand": num(get(r, "QuantityOnHand")), "on_sales_orders": num(get(r, "QuantityOnSalesOrder")),
                "reserved_for_builds": 0, "on_purchase_orders": num(get(r, "QuantityOnOrder")), "to_be_built": 0, "pending_transfers": 0,
                "reorder_point": num(get(r, point)),
            } for r in res.rows)
    for row in rows:
        row["available"] = round2(row["on_hand"] - row["on_sales_orders"] - row["reserved_for_builds"])
    return rows, truncated


@tool(
    "qb_stock",
    "Stock by item and site",
    "On hand, on sales orders, available and on purchase orders for each inventory and assembly item, per site when "
    "inventory sites are on (Advanced Inventory). Available is on hand minus open sales orders and parts reserved for "
    "pending builds, as QuickBooks counts it. A row is oversold when available is below zero, and at the reorder point "
    "when available plus what is on order is at or under it; such rows show what the other sites have available.",
    {
        "items": Arg("list", "Exact item names (full names); every active inventory and assembly item when left out"),
        "site": Arg("str", "Only this site"),
        "short": Arg("bool", "Only rows that are oversold or at the reorder point", default=False),
        "limit": Arg("int", "Most rows", default=200, min=1, max=2000),
    },
)
def stock(ctx: Ctx, a: dict) -> dict:
    by_site = company_facts(ctx).uses_sites
    names = a["items"] or []
    read = CAP * 2 if a["short"] else a["limit"]
    site = MISSING
    if a["site"]:
        if not by_site:
            raise ToolError("Inventory sites are off in this company file (they need Advanced Inventory), so stock is company-wide: leave out site.")
        known = [x["name"] for x in inventory_sites(ctx)]
        site = next((x for x in known if x.lower() == a["site"].lower()), MISSING)
        if site is MISSING:
            raise ToolError(f'No site named "{a["site"]}". Sites: {", ".join(known)}.')
    rows, truncated = stock_rows(ctx, names, site, read)
    for row in rows:
        coming = row["available"] + row["on_purchase_orders"] + row["to_be_built"]
        row["status"] = "oversold" if row["available"] < 0 else "reorder" if row["reorder_point"] > 0 and coming <= row["reorder_point"] else MISSING
        row["coming"] = coming
    if by_site and not a["site"]:
        for row in rows:
            if row["status"] is not MISSING:
                row["available_elsewhere"] = round2(sum(max(0, r["available"]) for r in rows if r["item"] == row["item"] and r is not row))
    found = {r["item"].lower() for r in rows}
    shown = [r for r in rows if r["status"] is not MISSING] if a["short"] else rows
    if a["short"]:
        shown.sort(key=lambda r: (0, r["available"]) if r["status"] == "oversold" else (1, r["coming"] - r["reorder_point"]))
    order = ["item", "site", "on_hand", "on_sales_orders", "reserved_for_builds", "available", "on_purchase_orders", "to_be_built", "pending_transfers", "reorder_point", "status", "available_elsewhere"]
    optional = ("reserved_for_builds", "to_be_built", "pending_transfers", "reorder_point", "available_elsewhere")
    return {
        "as_of": ctx.today(),
        "by_site": by_site,
        "oversold": sum(1 for r in rows if r["status"] == "oversold"),
        "at_reorder_point": sum(1 for r in rows if r["status"] == "reorder"),
        "rows": [{k: r[k] for k in order if k in r and r[k] is not MISSING and not (k in optional and not r[k])} for r in shown[:a["limit"]]],
        **({"more_rows": len(shown) - a["limit"]} if len(shown) > a["limit"] else {}),
        **({"not_found": [n for n in names if n.lower() not in found]} if any(n.lower() not in found for n in names) else {}),
        **({"note": f"Only the first {read} rows were checked."} if truncated else {}),
    }


def _order_lines(order: dict, base: str) -> list[dict]:
    """An order's lines, the lines inside groups included."""
    lines = _many(get(order, f"{base}LineRet"))
    for group in _many(get(order, f"{base}LineGroupRet")):
        lines += _many(get(group, f"{base}LineRet"))
    return lines


@tool(
    "qb_backorders",
    "Which open orders can ship",
    "Open sales-order lines checked against stock the way QuickBooks Enterprise's Sales Order Fulfillment Worksheet "
    "does: stock goes to the oldest orders first, at each line's site when inventory sites are on. Lists the lines that "
    "can ship in full now, and the short lines with the purchase orders on the way, when they should cover them, and "
    "the free stock at other sites. Use it to plan picking and transfers, and to tell customers when the rest will come.",
    {
        "customer": Arg("str", "Only this customer's orders"),
        "from": date("Oldest order date to read (default: a year ago)"),
        "limit": Arg("int", "Most lines in each list", default=50, min=1, max=1000),
    },
)
def backorders(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    start = a["from"] or add_days(today, -365)
    by_site = company_facts(ctx).uses_sites
    res = ctx.qb.query("SalesOrderQueryRq", {
        "TxnDateRangeFilter": {"FromTxnDate": start},
        **({"EntityFilter": {"FullNameWithChildren": a["customer"]}} if a["customer"] else {}),
        "IncludeLineItems": True,
    }, CAP, page_size=200)
    default = next((x["name"] for x in inventory_sites(ctx) if x["default"]), "") if by_site else ""
    lines: list[dict] = []
    open_orders = 0
    for so in res.rows:
        if truthy(get(so, "IsManuallyClosed")) or truthy(get(so, "IsFullyInvoiced")):
            continue
        open_orders += 1
        for line in _order_lines(so, "SalesOrder"):
            left = round2(num(get(line, "Quantity")) - num(get(line, "Invoiced")))
            if left > 0 and not truthy(get(line, "IsManuallyClosed")) and truthy(get(line, "ItemRef")):
                lines.append({
                    "so": get(so, "RefNumber"), "so_id": get(so, "TxnID"), "date": text(get(so, "TxnDate")), "ship_date": get(so, "ShipDate"),
                    "customer": ref_name(get(so, "CustomerRef")), "item": ref_name(get(line, "ItemRef")),
                    "site": nz(ref_name(get(line, "InventorySiteRef")), default) if by_site else MISSING, "open_qty": left,
                })
    names = sorted({x["item"] for x in lines})
    kinds = {text(get(r, "FullName")).lower(): get(r, "_type") for r in ctx.qb.query("ItemQueryRq", {"FullName": names, "IncludeRetElement": ["FullName"]}, len(names) + 1).rows} if names else {}
    lines = [x for x in lines if kinds.get(x["item"].lower()) in ("ItemInventory", "ItemInventoryAssembly")]
    stock, truncated = stock_rows(ctx, sorted({x["item"] for x in lines}), MISSING, CAP * 2) if lines else ([], False)

    def key(item: str, site: Any) -> tuple:
        return item.lower(), text(nz(site, "")).lower()

    asked: dict[tuple, float] = {}
    for x in lines:
        asked[key(x["item"], x["site"])] = asked.get(key(x["item"], x["site"]), 0) + x["open_qty"]
    # Open orders older than the ones read still come first: QuickBooks counts them in its quantity on sales orders.
    pool = {key(r["item"], r.get("site")): r["on_hand"] - r["reserved_for_builds"] - max(0, r["on_sales_orders"] - asked.get(key(r["item"], r.get("site")), 0)) for r in stock}
    lines.sort(key=lambda x: (x["date"], text(x["so"])))
    for x in lines:
        k = key(x["item"], x["site"])
        x["can_ship"] = round2(min(x["open_qty"], max(0, pool.get(k, 0))))
        x["short_qty"] = round2(x["open_qty"] - x["can_ship"])
        pool[k] = pool.get(k, 0) - x["open_qty"]
    short = [x for x in lines if x["short_qty"] > 0]
    incoming: dict[str, list[dict]] = {}
    if short:
        wanted = {x["item"].lower() for x in short}
        for po in ctx.qb.query("PurchaseOrderQueryRq", {"TxnDateRangeFilter": {"FromTxnDate": add_days(today, -365)}, "IncludeLineItems": True}, CAP, page_size=200).rows:
            if truthy(get(po, "IsManuallyClosed")) or truthy(get(po, "IsFullyReceived")):
                continue
            for line in _order_lines(po, "PurchaseOrder"):
                item = text(ref_name(get(line, "ItemRef")))
                left = round2(num(get(line, "Quantity")) - num(get(line, "ReceivedQuantity")))
                if item.lower() in wanted and left > 0 and not truthy(get(line, "IsManuallyClosed")):
                    incoming.setdefault(item.lower(), []).append({
                        "po": get(po, "RefNumber"), "vendor": ref_name(get(po, "VendorRef")),
                        "expected": text(nz(get(po, "ExpectedDate"), get(po, "DueDate"), get(po, "TxnDate"))), "qty": left,
                    })
        for pos in incoming.values():
            pos.sort(key=lambda p: p["expected"])
    # The earliest purchase orders cover the oldest short lines.
    left_on = {k: [dict(p) for p in v] for k, v in incoming.items()}
    for x in short:
        need = x["short_qty"]
        for p in left_on.get(x["item"].lower(), []):
            take = min(need, p["qty"])
            p["qty"] -= take
            need = round2(need - take)
            if take > 0:
                x["eta"] = p["expected"]
            if need <= 0:
                break
        if need > 0:
            x.pop("eta", None)
            x["not_on_order"] = need
        x["on_order"] = incoming.get(x["item"].lower(), [])[:3]
        if by_site:
            x["free_at_other_sites"] = round2(sum(max(0, v) for (item, site), v in pool.items() if item == x["item"].lower() and site != text(x["site"]).lower()))
    ready = [x for x in lines if x["short_qty"] == 0]

    def show(x: dict, keys: tuple) -> dict:
        return {k: x[k] for k in keys if k in x and x[k] is not MISSING and x[k] != []}

    base = ("so", "so_id", "date", "ship_date", "customer", "item", "site", "open_qty")
    return {
        "as_of": today,
        "by_site": by_site,
        "orders": {"from": start, "read": len(res.rows), "open": open_orders},
        "summary": {"ready_lines": len(ready), "short_lines": len(short), "orders_short": len({x["so_id"] for x in short}),
                    "customers_short": len({x["customer"] for x in short}), "short_lines_not_on_order": sum(1 for x in short if "not_on_order" in x)},
        "ready": [show(x, base) for x in ready[:a["limit"]]],
        "short": [show(x, (*base, "can_ship", "short_qty", "eta", "not_on_order", "on_order", "free_at_other_sites")) for x in short[:a["limit"]]],
        **({"more_ready": len(ready) - a["limit"]} if len(ready) > a["limit"] else {}),
        **({"more_short": len(short) - a["limit"]} if len(short) > a["limit"] else {}),
        **({"note": f"Only the first {CAP} sales orders since {start} were read."} if res.truncated else {}),
        **({"note_stock": "Stock was read for the first items only."} if truncated else {}),
    }


@tool(
    "qb_purchasing",
    "Purchase orders to chase",
    "The buyer's worklist: purchase order lines past their expected date and still not received, the open order value "
    "per vendor, goods received on an item receipt whose bill was never entered (received, not billed), and a "
    "three-way match: bills against the order they were made from (cost and quantity), and bills for items an open "
    "order waits for that were never linked to it. Use it to chase vendors, before paying bills, or before month end.",
    {
        "vendor": Arg("str", "Only this vendor"),
        "from": date("Oldest order date to read (default: a year ago)"),
        "limit": Arg("int", "Most lines listed in each part", default=50, min=1, max=1000),
    },
)
def purchasing(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    start = a["from"] or add_days(today, -365)
    who = {"EntityFilter": {"FullName": [a["vendor"]]}} if a["vendor"] else {}
    res = ctx.qb.query("PurchaseOrderQueryRq", {"TxnDateRangeFilter": {"FromTxnDate": start}, **who, "IncludeLineItems": True}, CAP, page_size=200)
    lines: list[dict] = []
    for po in res.rows:
        if truthy(get(po, "IsManuallyClosed")) or truthy(get(po, "IsFullyReceived")):
            continue
        expected = text(nz(get(po, "ExpectedDate"), get(po, "DueDate"), get(po, "TxnDate")))
        for line in _order_lines(po, "PurchaseOrder"):
            left = round2(num(get(line, "Quantity")) - num(get(line, "ReceivedQuantity")))
            if left > 0 and not truthy(get(line, "IsManuallyClosed")) and truthy(get(line, "ItemRef")):
                lines.append({"po": get(po, "RefNumber"), "vendor": text(ref_name(get(po, "VendorRef"))), "item": ref_name(get(line, "ItemRef")),
                              "open_qty": left, "value": round2(left * num(get(line, "Rate"))), "ordered": get(po, "TxnDate"), "expected": expected,
                              **({"days_late": days_between(expected, today)} if expected < today else {})})
    receipts = [{"receipt": get(r, "RefNumber"), "vendor": text(ref_name(get(r, "VendorRef"))), "date": get(r, "TxnDate"),
                 "days": days_between(text(get(r, "TxnDate")), today), "amount": num(get(r, "TotalAmount")), "txn_id": get(r, "TxnID")}
                for r in ctx.qb.query("ItemReceiptQueryRq", {"TxnDateRangeFilter": {"FromTxnDate": start}, **who,
                                                             "IncludeRetElement": ["TxnID", "TxnDate", "RefNumber", "VendorRef", "TotalAmount"]}, CAP).rows]
    # Three-way match. A bill linked to an order is checked against the order's lines (cost and quantity); a bill for an
    # item that an open order from the same vendor is waiting for, but not linked to it, leaves that order open, so
    # the buyer chases goods that came and reorder counts them as still on order.
    orders = {text(get(po, "TxnID")): po for po in res.rows}
    bills = ctx.qb.query("BillQueryRq", {"TxnDateRangeFilter": {"FromTxnDate": start}, **who, "IncludeLineItems": True, "IncludeLinkedTxns": True}, CAP, page_size=200).rows
    linked = {text(get(b, "TxnID")): [text(get(x, "TxnID")) for x in _many(get(b, "LinkedTxn")) if get(x, "TxnType") == "PurchaseOrder"] for b in bills}
    missing = sorted({i for ids in linked.values() for i in ids if i not in orders})
    if missing:
        orders.update({text(get(po, "TxnID")): po for po in ctx.qb.query("PurchaseOrderQueryRq", {"TxnID": missing, "IncludeLineItems": True}, len(missing) + 1).rows})
    costs, billed, unlinked = [], {}, {}
    for b in bills:
        bill_lines = [l for l in _many(get(b, "ItemLineRet")) + [x for g in _many(get(b, "ItemGroupLineRet")) for x in _many(get(g, "ItemLineRet"))]
                      if truthy(get(l, "ItemRef"))]
        for po in (orders[i] for i in linked[text(get(b, "TxnID"))] if i in orders):
            rate = {ref_name(get(l, "ItemRef")): num(get(l, "Rate")) for l in _order_lines(po, "PurchaseOrder") if truthy(get(l, "ItemRef"))}
            for l in bill_lines:
                item, qty, cost = ref_name(get(l, "ItemRef")), num(get(l, "Quantity")), num(get(l, "Cost"))
                key = (text(get(po, "RefNumber")), item)
                billed.setdefault(key, {"po": key[0], "vendor": text(ref_name(get(b, "VendorRef"))), "item": item, "bills": [], "billed": 0.0,
                                        "ordered": sum_of(_order_lines(po, "PurchaseOrder"), lambda x: num(get(x, "Quantity")) if ref_name(get(x, "ItemRef")) == item else 0)})
                billed[key]["billed"] = round2(billed[key]["billed"] + qty)
                billed[key]["bills"].append(get(b, "RefNumber"))
                if item in rate and abs(cost - rate[item]) >= 0.005:
                    costs.append({"bill": get(b, "RefNumber"), "po": key[0], "vendor": billed[key]["vendor"], "item": item, "quantity": qty,
                                  "po_rate": rate[item], "bill_cost": cost, "difference": round2((cost - rate[item]) * qty)})
        if linked[text(get(b, "TxnID"))]:
            continue
        for l in bill_lines:
            item = ref_name(get(l, "ItemRef"))
            for x in lines:
                if x["vendor"] == text(ref_name(get(b, "VendorRef"))) and x["item"] == item and text(x["ordered"]) <= text(get(b, "TxnDate")):
                    u = unlinked.setdefault((x["po"], item), {"po": x["po"], "vendor": x["vendor"], "item": item, "po_open_qty": x["open_qty"], "bills": [], "billed": 0.0})
                    u["bills"].append(get(b, "RefNumber"))
                    u["billed"] = round2(u["billed"] + num(get(l, "Quantity")))
    over = [{**x, "bills": list(dict.fromkeys(x["bills"]))} for x in billed.values() if x["billed"] > x["ordered"]]
    not_linked = [{**u, "bills": list(dict.fromkeys(u["bills"])),
                   "issue": f"Billed {num_str(u['billed'])} on {', '.join(dict.fromkeys(u['bills']))} without a link to {u['po']}, which still waits for {num_str(u['po_open_qty'])}"}
                  for u in unlinked.values()]
    for x in lines:
        hit = unlinked.get((x["po"], x["item"]))
        if hit and "days_late" in x:
            x["possibly_billed_on"] = list(dict.fromkeys(hit["bills"]))
    late = sorted((x for x in lines if "days_late" in x), key=lambda x: (x["expected"], text(nz(x["po"], ""))))
    receipts.sort(key=lambda x: text(x["date"]))
    vendors = []
    for v in dict.fromkeys(x["vendor"] for x in lines):
        mine = [x for x in lines if x["vendor"] == v]
        overdue = [x for x in mine if "days_late" in x]
        vendors.append({"vendor": v, "open_orders": len({x["po"] for x in mine}), "open_value": sum_of(mine, lambda x: x["value"]),
                        **({"late_lines": len(overdue), "late_value": sum_of(overdue, lambda x: x["value"]),
                            "most_days_late": max(x["days_late"] for x in overdue)} if overdue else {})})
    vendors.sort(key=lambda v: (v.get("late_value", 0), v["open_value"]), reverse=True)
    return {
        "as_of": today,
        "open_orders": {"orders": len({x["po"] for x in lines}), "lines": len(lines), "value": sum_of(lines, lambda x: x["value"]),
                        "late_lines": len(late), "late_value": sum_of(late, lambda x: x["value"])},
        "by_vendor": vendors[:a["limit"]],
        "late": late[:a["limit"]],
        **({"more_late": len(late) - a["limit"]} if len(late) > a["limit"] else {}),
        "received_not_billed": {"receipts": len(receipts), "total": sum_of(receipts, lambda x: x["amount"]), "oldest_first": receipts[:a["limit"]]},
        "three_way_match": {
            "bills_read": len(bills), "bills_linked_to_orders": sum(bool(v) for v in linked.values()),
            "cost_differences": sorted(costs, key=lambda x: -abs(x["difference"]))[:a["limit"]],
            "billed_beyond_ordered": over[:a["limit"]],
            "bills_not_linked": not_linked[:a["limit"]],
            **({"next_step": "For each bill not linked: open the purchase order and close the lines the bill covered (or link the "
                             "bill to it), so the order stops showing as on order."} if not_linked else {}),
        },
        **({"note": f"Only the first {CAP} purchase orders since {start} were read."} if res.truncated else {}),
    }


# ---------------------------------------------------------------- qb_stock_history

HISTORY = ["TxnType", "Date", "RefNumber", "Name", "Quantity", "QuantityOnHand", "AverageCost", "TxnID"]
PURCHASES = {"bill", "itemreceipt", "check", "creditcardcharge"}


@tool(
    "qb_stock_history",
    "Why an item's stock is what it is",
    "One inventory or assembly item's movements from QuickBooks' Inventory Valuation Detail report, with the quantity "
    "on hand after each, and every time the item went below zero: the transaction that took it there, how low it went "
    "and what brought it back. A sale entered before the purchase that supplied it is costed at the average cost of "
    "the day, and the purchase corrects cost of goods sold on its own date, so each time matters, not only today's "
    "quantity. Use it when an item shows negative stock or its cost of goods sold looks wrong.",
    {
        "item": Arg("str", "The item's full name", required=True),
        "from": date("First date (default: a year before to)"),
        "to": date("Last date (default: today)"),
        "limit": Arg("int", "Most movements listed, the latest", default=50, min=1, max=1000),
    },
)
def stock_history(ctx: Ctx, a: dict) -> dict:
    to = a["to"] or ctx.today()
    start = a["from"] or add_days(to, -365)
    if start > to:
        raise ToolError("from is after to.")
    if days_between(start, to) > 366:
        raise ToolError("One year at a time: narrow from/to.")
    item = _named(ctx, "Item", a["item"], "FullName", ("QuantityOnHand", "QuantityOnOrder"))
    name = text(get(item, "FullName"))
    if get(item, "_type") not in ("ItemInventory", "ItemInventoryAssembly"):
        raise ToolError(f'"{name}" is not an inventory or assembly item, so QuickBooks keeps no stock for it.')
    r = ctx.qb.report("GeneralDetailReportQueryRq", {
        "GeneralDetailReportType": "InventoryValuationDetail",
        "ReportPeriod": {"FromReportDate": start, "ToReportDate": to},
        "ReportItemFilter": {"FullName": [name]},
        "IncludeColumn": HISTORY,
    })
    col = {t: next((i for i, c in enumerate(r.columns) if c.type == t), -1) for t in HISTORY}
    if col["Quantity"] < 0 or col["QuantityOnHand"] < 0:
        raise ToolError("QuickBooks sent the Inventory Valuation Detail without its quantity columns. Columns: " + ", ".join(c.title or c.type for c in r.columns) + ".")
    moves = []
    for row in r.rows:
        v = {t: _at(row.values, i) for t, i in col.items()}
        if row.kind == "data" and truthy(v["Date"]):
            moves.append({"date": text(v["Date"]), "type": v["TxnType"], "ref": v["RefNumber"], "name": v["Name"], "qty": num(v["Quantity"]),
                          "on_hand": num(v["QuantityOnHand"]), "avg_cost": v["AverageCost"], "id": v["TxnID"]})

    def show(m: dict) -> dict:
        return {k: x for k, x in m.items() if x is not MISSING and x != ""}

    def step(m: dict, before: float) -> dict:
        return show({"date": m["date"], "type": m["type"], "ref": m["ref"], "name": m["name"], "qty": m["qty"], "on_hand_before": before, "on_hand_after": m["on_hand"], "id": m["id"]})

    # Each time the item went below zero: what took it there, how low it went, and what brought it back.
    opening = round2(moves[0]["on_hand"] - moves[0]["qty"]) if moves else MISSING
    times: list[dict] = []
    now: dict | None = {"from": start, "started_before_period": True, "lowest": opening} if moves and opening < 0 else None
    before = opening
    for m in moves:
        if now is None and m["on_hand"] < 0:
            now = {"from": m["date"], "started_by": step(m, before), "lowest": m["on_hand"]}
        elif now is not None:
            now["lowest"] = min(now["lowest"], m["on_hand"])
            if m["on_hand"] >= 0:
                times.append({**now, "days": days_between(now["from"], m["date"]), "ended_by": step(m, before)})
                now = None
        before = m["on_hand"]
    if now is not None:
        times.append({**now, "days": days_between(now["from"], to), "still_below_zero": True})
    qoh, end = get(item, "QuantityOnHand"), moves[-1]["on_hand"] if moves else MISSING
    steps = []
    if any(re.sub(r"[^a-z]", "", text(t.get("ended_by", {}).get("type")).lower()) in PURCHASES for t in times):
        steps.append("A purchase brought it back: if the goods were in stock when sold, date that purchase on or before the sale that took the item below zero, so the cost lands with the sales.")
    if times and times[-1].get("still_below_zero"):
        steps.append("It is still below zero: enter the item receipt or bill for goods that came in, move a purchase entered on the Expenses tab to the Items tab, "
                     "or after a count adjust the quantity (Vendors > Inventory Activities > Adjust Quantity/Value on Hand).")
    if times and times[0].get("started_before_period"):
        steps.append(f"It was already below zero on {start}: run again with an earlier from.")
    if times:
        steps.append("Fix each time it went below zero, not only today's quantity (Intuit's advice); back up first, mind the closing date, and agree date changes with the accountant.")
    notes = []
    if not moves:
        notes.append(f"No stock movements between {start} and {to}.")
    elif to == ctx.today() and is_num(qoh) and abs(qoh - end) >= 0.005:
        notes.append(f"The item list shows {num_str(qoh)} on hand and the report {num_str(end)} on {to}: look for transactions dated after {to}.")
    return {
        "item": name,
        "period": {"from": start, "to": to},
        "on_hand": show({"at_start": opening, "at_end": end, "item_list": qoh}),
        "on_order": num(get(item, "QuantityOnOrder")),
        "movements": len(moves),
        "in": sum_of(moves, lambda m: max(m["qty"], 0)),
        "out": sum_of(moves, lambda m: min(m["qty"], 0)),
        "below_zero": times,
        "moves": [show(m) for m in moves[-a["limit"]:]],
        **({"more_moves": len(moves) - a["limit"]} if len(moves) > a["limit"] else {}),
        **({"notes": notes} if notes else {}),
        **({"next_step": " ".join(steps)} if steps else {}),
    }


# ---------------------------------------------------------------- qb_1099

def irs_1099_threshold(year: int, box: str) -> float:
    """The IRS reporting threshold for a 1099 box: $600 for payments made through 2025, $2,000 from 2026 (One Big
    Beautiful Bill Act; IRS Publication 1099 (2026)), indexed for inflation after 2026. Royalties stay at $10 and
    attorneys' gross proceeds at $600."""
    b = box.lower()
    return 10 if "royalt" in b else 600 if "attorney" in b or "gross proceeds" in b or year <= 2025 else 2000


def _item_account(item: Any) -> Any:
    """The account an item's purchase line posts to."""
    return ref_name(nz(get(get(item, "SalesAndPurchase"), "ExpenseAccountRef"), get(get(item, "SalesOrPurchase"), "AccountRef"),
                       get(item, "AssetAccountRef"), get(item, "COGSAccountRef"), MISSING))


@tool(
    "qb_1099",
    "1099 worklist",
    "The vendors to send a 1099 for a calendar year: what each was paid by check or cash on the accounts the company "
    "file maps to 1099 boxes, the way QuickBooks' 1099 wizard counts it (card payments are left out: the card company "
    "reports them), who is over the IRS threshold for that year, whose tax ID or address is missing, and vendors not "
    "marked for 1099 that were paid over the threshold on mapped accounts. Tax IDs are never shown, only whether one is "
    "on file.",
    {
        "year": Arg("int", "Calendar year of the payments (default: last year until April, then this year so far)", min=2000, max=2100),
        "limit": Arg("int", "Most vendors in each list", default=100, min=1, max=1000),
    },
)
def form_1099(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    year = a["year"] or int(today[:4]) - (1 if int(today[5:7]) <= 4 else 0)
    span = {"TxnDateRangeFilter": {"FromTxnDate": f"{year}-01-01", "ToTxnDate": f"{year}-12-31"}}
    notes: list[str] = []
    try:
        maps = ctx.qb.query("Form1099CategoryAccountMappingQueryRq", {}, 50).rows
    except QbStatusError:
        maps = []
    boxes: dict[str, tuple[str, Any]] = {}  # account, lower case -> (box, the file's threshold)
    mapping: dict[str, list[str]] = {}  # box -> its accounts
    for m in maps:
        for c in _many(get(m, "CategoryAccountMapping")):
            box = f"{text(nz(get(m, 'FormType'), '1099'))} {text(get(c, 'MappingCategory'))}"
            for acct in _many(get(c, "AccountRef")):
                boxes[text(ref_name(acct)).lower()] = (box, get(c, "Threshold"))
                mapping.setdefault(box, []).append(text(ref_name(acct)))
    if not boxes:
        notes.append("No 1099 account mapping was read (Edit > Preferences > Tax: 1099), so payments on every account count; "
                     "QuickBooks' 1099 wizard counts only mapped accounts.")
    for box, kept in dict(boxes.values()).items():
        if is_num(kept) and kept != irs_1099_threshold(year, box):
            notes.append(f"The file's 1099 mapping uses {fmt(kept)} for {box}; for payments made in {year} the IRS threshold is {fmt(irs_1099_threshold(year, box))}.")
    if year >= 2027:
        notes.append("From 2027 the $2,000 threshold is indexed for inflation: check the IRS figure for the year.")

    vendors = {text(get(v, "Name")): v for v in ctx.qb.query("VendorQueryRq", {"ActiveStatus": "All", "IncludeRetElement": [
        "Name", "IsActive", "IsVendorEligibleFor1099", "VendorTaxIdent", "VendorAddress"]}, CAP).rows}
    read = [ctx.qb.query("CheckQueryRq", {**span, "IncludeLineItems": True}, CAP), ctx.qb.query("BillPaymentCheckQueryRq", span, CAP)]
    checks, payments = read[0].rows, read[1].rows
    if any(r.truncated for r in read):
        notes.append(f"Only the first {CAP} checks and {CAP} bill payments of {year} were read, so some totals are low.")
    paid_bills: dict[str, dict] = {}
    applied = sorted({text(get(x, "TxnID")) for p in payments for x in _many(get(p, "AppliedToTxnRet"))})
    for i in range(0, len(applied), 200):
        part = applied[i:i + 200]
        paid_bills.update({text(get(b, "TxnID")): b for b in ctx.qb.query("BillQueryRq", {"TxnID": part, "IncludeLineItems": True}, len(part) + 1).rows})

    def lines_of(t: dict) -> list[dict]:
        return _many(get(t, "ExpenseLineRet")) + _many(get(t, "ItemLineRet")) + [x for g in _many(get(t, "ItemGroupLineRet")) for x in _many(get(g, "ItemLineRet"))]

    names = sorted({text(ref_name(get(l, "ItemRef"))) for t in [*checks, *paid_bills.values()] for l in lines_of(t) if truthy(get(l, "ItemRef"))})
    item_acct = {text(get(r, "FullName")): _item_account(r) for r in ctx.qb.query("ItemQueryRq", {"FullName": names}, len(names) + 1).rows} if names else {}
    counted: dict[str, dict[str, float]] = {}  # vendor -> box -> amount
    elsewhere: dict[str, float] = {}

    def count(vendor: str, t: dict, share: float) -> None:
        for l in lines_of(t):
            acct = text(nz(ref_name(get(l, "AccountRef")), item_acct.get(text(ref_name(get(l, "ItemRef")))), ""))
            amount = num(get(l, "Amount")) * share
            box = boxes.get(acct.lower(), (None,))[0] if boxes else "all accounts"
            if box:
                counted.setdefault(vendor, {})[box] = counted.get(vendor, {}).get(box, 0) + amount
            else:
                elsewhere[vendor] = elsewhere.get(vendor, 0) + amount

    for t in checks:
        if text(ref_name(get(t, "PayeeEntityRef"))) in vendors:
            count(text(ref_name(t["PayeeEntityRef"])), t, 1)
    for p in payments:
        vendor = text(ref_name(get(p, "PayeeEntityRef")))
        for x in _many(get(p, "AppliedToTxnRet")):
            bill = paid_bills.get(text(get(x, "TxnID")))
            total = sum_of(lines_of(bill), lambda l: num(get(l, "Amount"))) if bill else 0
            if vendor in vendors and total:
                count(vendor, bill, num(get(x, "Amount")) / total)
    card: dict[str, float] = {}
    for kind in ("BillPaymentCreditCardQueryRq", "CreditCardChargeQueryRq"):
        for t in ctx.qb.query(kind, span, CAP).rows:
            v = text(ref_name(get(t, "PayeeEntityRef")))
            card[v] = card.get(v, 0) + num(get(t, "Amount"))

    rights = get(nz(ctx.qb.request("PreferencesQueryRq", {"IncludeRetElement": ["CurrentAppAccessRights"]}).body.get("PreferencesRet", MISSING), {}), "CurrentAppAccessRights")
    shared = get(rights, "IsPersonalDataAccessAllowed") is True
    if not shared:
        notes.append("QuickBooks does not share personal data with this app, so tax IDs read as \"not shared\"; the owner can allow it "
                     "in Edit > Preferences > Integrated Applications.")
    rows = []
    for name, v in vendors.items():
        boxes_paid = {b: round2(x) for b, x in counted.get(name, {}).items() if round2(x)}
        marked = get(v, "IsVendorEligibleFor1099") is True
        if not boxes_paid and not marked:
            continue
        over = [b for b, x in boxes_paid.items() if x >= irs_1099_threshold(year, b)]
        addr = nz(get(v, "VendorAddress"), {})
        gaps = [k for k in ("Addr1", "City", "State", "PostalCode") if not truthy(get(addr, k))]
        rows.append({
            "vendor": name, "marked_1099": marked, "counted": sum_of(boxes_paid.values(), lambda x: x),
            "by_box": [{"box": b, "amount": x, "irs_threshold": irs_1099_threshold(year, b)} for b, x in boxes_paid.items()],
            "over_threshold": bool(over),
            "tax_id": "on file" if truthy(get(v, "VendorTaxIdent")) else "missing" if shared else "not shared",
            "address": "complete" if not gaps else "missing " + ", ".join(gaps),
            **({"other_accounts": round2(elsewhere[name])} if round2(elsewhere.get(name, 0)) else {}),
            **({"paid_by_card": round2(card[name])} if round2(card.get(name, 0)) else {}),
            **({"inactive": True} if get(v, "IsActive") is False else {}),
        })
    rows.sort(key=lambda r: -r["counted"])
    send = [r for r in rows if r["marked_1099"] and r["over_threshold"]]
    check = [r for r in rows if not r["marked_1099"] and r["over_threshold"]]
    return {
        "year": year, **({"so_far": True} if year == int(today[:4]) else {}),
        "irs_threshold": irs_1099_threshold(year, ""),
        "mapping": [{"box": b, "accounts": accounts} for b, accounts in mapping.items()],
        "summary": {"marked_1099": sum(r["marked_1099"] for r in rows), "send_1099": len(send),
                    "missing_tax_id": sum(r["tax_id"] == "missing" for r in send), "incomplete_address": sum(r["address"] != "complete" for r in send),
                    "not_marked_but_over": len(check)},
        "send_1099": send[: a["limit"]],
        "check_these": [{**r, "why": "paid over the threshold on mapped accounts but not marked for 1099: check whether they should get one "
                                     "(a corporation usually does not)"} for r in check[: a["limit"]]],
        "under_threshold": [r["vendor"] for r in rows if r["marked_1099"] and not r["over_threshold"]][: a["limit"]],
        **({"notes": notes} if notes else {}),
        "next_step": "Ask for a W-9 where the tax ID or address is missing and fix the vendor's Tax Settings and Address; then compare "
                     f"with QuickBooks' own figures: qbd report 1099Detail --from {year}-01-01 --to {year}-12-31.",
    }


REGISTER = ["TxnID", "TxnType", "Date", "RefNumber", "Name", "Memo", "Account", "ClearedStatus", "Amount"]


def cleared_status(v: Any) -> str:
    """The Clr column of a report: blank when not cleared, Pending (*) when marked in an unfinished reconciliation."""
    s = text(nz(v, "")).strip().lower()
    return "pending" if s in ("pending", "*") else "not_cleared" if s in ("", "notcleared", "not cleared") else "cleared"


@tool(
    "qb_uncleared",
    "Uncleared bank and card items",
    "Bank and credit card transactions still not cleared after some days (checks never cashed, deposits the bank never "
    "saw, entries made twice), oldest first and with a likely twin when there is one, plus items marked in a "
    "reconciliation that was left unfinished. Use it before reconciling, or when a reconciliation does not match.",
    {
        "account": Arg("str", "One bank or credit card account; every one when left out"),
        "older_than_days": Arg("int", "Not cleared for at least this many days", default=30, min=0, max=3650),
        "from": date("Oldest date to look at (default: a year ago)"),
        "limit": Arg("int", "Most items listed", default=100, min=1, max=1000),
    },
)
def uncleared(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    start = a["from"] or add_days(today, -365)
    cutoff = add_days(today, -a["older_than_days"])
    if a["account"]:
        acct = ctx.qb.query("AccountQueryRq", {"FullName": [a["account"]], "IncludeRetElement": ["FullName", "AccountType"]}, 1).rows
        if not acct or text(get(acct[0], "AccountType")) not in ("Bank", "CreditCard"):
            raise ToolError(f'"{a["account"]}" is not a bank or credit card account.')
        filters = [{"FullName": [text(get(acct[0], "FullName"))]}]
    else:
        filters = [{"AccountTypeFilter": "Bank"}, {"AccountTypeFilter": "CreditCard"}]
    lines: list[dict] = []
    for f in filters:
        r = ctx.qb.report("GeneralDetailReportQueryRq", {
            "GeneralDetailReportType": "TxnDetailByAccount",
            "ReportPeriod": {"FromReportDate": start, "ToReportDate": today},
            "ReportAccountFilter": f,
            "IncludeColumn": REGISTER,
        })
        col = {t: next((i for i, c in enumerate(r.columns) if c.type == t), -1) for t in REGISTER}
        if col["ClearedStatus"] < 0:
            raise ToolError("QuickBooks sent the register without its Clr column, so cleared items cannot be told apart.")
        group = ""
        for row in r.rows:
            if row.kind == "text":
                group = row.label
            v = {t: _at(row.values, i) for t, i in col.items()}
            if row.kind != "data" or not truthy(v["Date"]):
                continue
            lines.append({"account": text(nz(v["Account"] or MISSING, group)), "date": text(v["Date"]), "type": v["TxnType"], "ref": v["RefNumber"],
                          "name": v["Name"], "memo": v["Memo"], "amount": num(v["Amount"]), "id": v["TxnID"], "status": cleared_status(v["ClearedStatus"])})

    def twin(x: dict) -> Any:
        same = [y for y in lines if y is not x and y["account"] == x["account"] and y["amount"] == x["amount"]
                and text(y["name"]).lower() == text(x["name"]).lower() and abs(days_between(y["date"], x["date"])) <= 7]
        return {"id": same[0]["id"], "date": same[0]["date"], "ref": same[0]["ref"], "status": same[0]["status"]} if same else MISSING

    def show(x: dict, **more: Any) -> dict:
        return {k: v for k, v in {**x, **more}.items() if k != "status" and v is not MISSING and v != ""}

    old = sorted((x for x in lines if x["status"] == "not_cleared" and x["date"] <= cutoff), key=lambda x: x["date"])
    pending = [x for x in lines if x["status"] == "pending"]
    accounts = []
    for name in dict.fromkeys(x["account"] for x in lines):
        mine = [x for x in old if x["account"] == name]
        accounts.append({"account": name, "uncleared": len(mine), "uncleared_total": sum_of(mine, lambda x: x["amount"]),
                         "pending": sum(1 for x in pending if x["account"] == name)})
    return {
        "as_of": today,
        "checked": {"from": start, "to": today, "transactions": len(lines)},
        "older_than_days": a["older_than_days"],
        "accounts": accounts,
        "uncleared": [show(x, days=days_between(x["date"], today), twin=twin(x)) for x in old[:a["limit"]]],
        **({"more_uncleared": len(old) - a["limit"]} if len(old) > a["limit"] else {}),
        **({"pending": [show(x) for x in pending[:a["limit"]]],
            "pending_note": "Marked in a reconciliation that was never finished: finish it, or undo it, in Banking > Reconcile."} if pending else {}),
    }


# ---------------------------------------------------------------- qb_price_check

PRICED = ("ItemService", "ItemInventory", "ItemInventoryAssembly", "ItemNonInventory", "ItemOtherCharge")


def level_price(level: Any, item: str, base: float) -> float:
    """What a price level makes of an item's standard price: a fixed percentage up or down for every item, or a
    custom price or percentage for the items it lists (the standard price for the others)."""
    if not isinstance(level, dict):
        return base
    if text(get(level, "PriceLevelType")) == "FixedPercentage":
        return round2(base * (1 + num(get(level, "PriceLevelFixedPercentage")) / 100))
    for x in _many(get(level, "PriceLevelPerItemRet")):
        if text(ref_name(get(x, "ItemRef"))).lower() == item.lower():
            if finite(get(x, "CustomPrice")):
                return num(get(x, "CustomPrice"))
            return round2(base * (1 + num(get(x, "CustomPricePercent")) / 100))
    return base


@tool(
    "qb_price_check",
    "Sales priced below the price list",
    "Invoice and sales receipt lines priced below what the item list and the customer's price level say, with what the "
    "difference cost, by customer, item and sales rep, plus the discount lines given openly. Catches discounts nobody "
    "approved and old prices typed by hand. Lines are compared with today's prices, so an item whose price changed since "
    "the sale shows up too; Advanced Pricing's price rules cannot be read through the SDK, so only price levels count.",
    {
        "from": date("First date (default: 90 days before to)"),
        "to": date("Last date (default: today)"),
        "customer": Arg("str", "One customer (and its jobs)"),
        "min_gap_percent": Arg("int", "Ignore lines less than this many percent below the expected price", default=1, min=0, max=100),
        "limit": Arg("int", "Most lines listed", default=25, min=1, max=500),
    },
)
def price_check(ctx: Ctx, a: dict) -> dict:
    to = nz(a["to"], ctx.today())
    start = nz(a["from"], add_days(to, -90))
    if start > to:
        raise ToolError("from is after to.")
    sales: list[tuple[str, dict, dict]] = []
    truncated = False
    for base in ("Invoice", "SalesReceipt"):
        res = ctx.qb.query(f"{base}QueryRq", {
            "TxnDateRangeFilter": {"FromTxnDate": start, "ToTxnDate": to},
            **({"EntityFilter": {"FullNameWithChildren": a["customer"]}} if a["customer"] else {}),
            "IncludeLineItems": True,
        }, CAP, page_size=200)
        truncated = truncated or res.truncated
        sales += [(base, t, line) for t in res.rows for line in _many(get(t, f"{base}LineRet"))]
    names = sorted({text(ref_name(get(line, "ItemRef"))) for _, _, line in sales if truthy(get(line, "ItemRef"))})
    items = {text(get(r, "FullName")).lower(): r for r in ctx.qb.query("ItemQueryRq", {
        "FullName": names, "IncludeRetElement": ["FullName", "SalesPrice", "SalesOrPurchase", "SalesAndPurchase"]}, len(names) + 1).rows} if names else {}
    customers = sorted({text(ref_name(get(t, "CustomerRef"))) for _, t, _ in sales if truthy(get(t, "CustomerRef"))})
    levels_off = ""
    try:
        level_of = {text(get(r, "FullName")).lower(): text(ref_name(get(r, "PriceLevelRef"))) for r in ctx.qb.query("CustomerQueryRq", {
            "FullName": customers, "IncludeRetElement": ["FullName", "PriceLevelRef"]}, len(customers) + 1).rows} if customers else {}
        levels = {text(get(r, "Name")).lower(): r for r in ctx.qb.query("PriceLevelQueryRq", {"ActiveStatus": "All"}, 1000).rows} if any(level_of.values()) else {}
    except QbStatusError as e:
        # Enterprise turns price levels off when Advanced Pricing's price rules are on, and refuses to name them (3220).
        if e.status_code != 3220:
            raise
        level_of, levels, levels_off = {}, {}, e.message

    checked = 0
    below: list[dict] = []
    discounts: list[float] = []
    for base, t, line in sales:
        item = text(ref_name(get(line, "ItemRef")))
        rec = items.get(item.lower())
        kind = get(rec, "_type") if rec else MISSING
        if kind == "ItemDiscount":
            discounts.append(num(get(line, "Amount")))
            continue
        # A line with a rate and no quantity is billed once.
        qty, rate, listed = num(nz(get(line, "Quantity"), 1)), get(line, "Rate"), num(item_price(rec, False)) if rec else 0
        if kind not in PRICED or not finite(rate) or qty <= 0 or listed <= 0:
            continue
        checked += 1
        customer = ref_name(get(t, "CustomerRef"))  # a cash sale may have none
        level = level_of.get(customer.lower(), "") if customer is not MISSING else ""
        expected = level_price(levels.get(level.lower()), item, listed)
        if rate < expected - 0.005 and rate <= expected * (1 - a["min_gap_percent"] / 100) + 1e-9:
            below.append({"type": base, "ref": get(t, "RefNumber"), "date": text(get(t, "TxnDate")), "customer": customer,
                          "rep": nz(ref_name(get(t, "SalesRepRef")), MISSING), "item": item, "quantity": qty, "rate": rate,
                          "expected": expected, **({"price_level": level} if level else {}), "gap": round2((expected - rate) * qty),
                          "txn_id": get(t, "TxnID")})
    below.sort(key=lambda x: -x["gap"])

    def by(key: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for x in below:
            if x.get(key) is not MISSING:
                groups.setdefault(x[key], []).append(x)
        rows = [{key: k, "lines": len(v), "gap": sum_of(v, lambda x: x["gap"])} for k, v in groups.items()]
        return sorted(rows, key=lambda r: -r["gap"])[:10]

    notes = []
    if levels_off:
        notes.append(f"Price levels are off in this company file (QuickBooks: \"{levels_off}\"), so lines are compared "
                     "with the item list price only. Price rules cannot be read through the SDK: a line priced by a rule "
                     "shows up here too.")
    if not checked:
        notes.append("No invoice or sales receipt lines with a listed item price in this period.")
    if below:
        notes.append("Lines are compared with today's prices: a line from before a price rise shows up too. Check a few "
                     "against the price in force on their date before raising them with the sales team.")
    return {
        "period": {"from": start, "to": to},
        "lines_checked": checked,
        "below_list": {"lines": len(below), "gap": sum_of(below, lambda x: x["gap"])},
        "by_customer": by("customer"),
        "by_item": by("item"),
        **({"by_rep": rows} if (rows := by("rep")) else {}),
        "lines": [{k: v for k, v in x.items() if v is not MISSING} for x in below[:a["limit"]]],
        **({"more_lines": len(below) - a["limit"]} if len(below) > a["limit"] else {}),
        **({"discount_lines": {"lines": len(discounts), "total": sum_of(discounts, lambda x: x)}} if discounts else {}),
        **({"truncated": True, "hint": f"More than {CAP} sales in the period: narrow from/to."} if truncated else {}),
        "notes": notes,
    }


# ---------------------------------------------------------------- qb_reconcile_check

# Deleted transactions that can take money out of or into a bank account.
BANK_DELETES = ["Check", "Deposit", "BillPaymentCheck", "JournalEntry", "SalesReceipt", "ReceivePayment", "SalesTaxPaymentCheck"]
US_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def report_day(v: Any) -> str:
    """The day in a report's time column, written 2026-10-20T14:05:00 or 10/20/2026 2:05:00 PM."""
    s = text(nz(v, "")).strip()
    if re.match(r"\d{4}-\d\d-\d\d", s):
        return s[:10]
    m = US_DATE.search(s)
    return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}" if m else ""


@tool(
    "qb_reconcile_check",
    "Why a reconciliation's beginning balance is off",
    "For a bank account whose beginning balance in Reconcile no longer matches the bank statement: adds up what "
    "QuickBooks counts as reconciled (its beginning balance), compares it with the bank's balance at the end of the last "
    "statement reconciled, and lists reconciled transactions changed or voided since that reconciliation and bank "
    "transactions deleted since then, which is where such a difference comes from. Nothing is changed.",
    {
        "account": Arg("str", "The bank account", required=True),
        "statement_date": Arg("date", "End date of the last statement reconciled", required=True),
        "bank_balance": Arg("str", "The bank's balance on that date, e.g. 12450.37 (the next statement's beginning balance)", required=True),
        "reconciled_on": date("The day that reconciliation was done (default: statement_date)"),
        "limit": Arg("int", "Most transactions listed", default=50, min=1, max=500),
    },
)
def reconcile_check(ctx: Ctx, a: dict) -> dict:
    bank = to_number(a["bank_balance"].replace(",", ""))
    if not finite(bank):
        raise ToolError(f'"{a["bank_balance"]}" is not an amount: give a number such as 12450.37.')
    since = nz(a["reconciled_on"], a["statement_date"])
    acct = ctx.qb.query("AccountQueryRq", {"FullName": [a["account"]], "IncludeRetElement": ["FullName", "AccountType"]}, 1).rows
    if not acct or text(get(acct[0], "AccountType")) != "Bank":
        raise ToolError(f'"{a["account"]}" is not a bank account.')
    name = text(get(acct[0], "FullName"))
    r = ctx.qb.report("GeneralDetailReportQueryRq", {
        "GeneralDetailReportType": "TxnDetailByAccount",
        "ReportDateMacro": "All",
        "ReportAccountFilter": {"FullName": [name]},
        "IncludeColumn": [*REGISTER, "ModifiedTime", "LastModifiedBy"],
    })
    col = {c.type: i for i, c in enumerate(r.columns)}
    if "ClearedStatus" not in col or "Amount" not in col:
        raise ToolError("QuickBooks sent the register without its Clr or Amount column.")
    reconciled: list[dict] = []
    for row in r.rows:
        v = {t: _at(row.values, i) for t, i in col.items()}
        if row.kind != "data" or not truthy(v.get("Date")) or cleared_status(v["ClearedStatus"]) != "cleared":
            continue
        reconciled.append({"date": text(v["Date"]), "type": v.get("TxnType", MISSING), "ref": v.get("RefNumber", MISSING), "name": v.get("Name", MISSING),
                           "amount": num(v["Amount"]), "modified": report_day(v.get("ModifiedTime")) or MISSING,
                           "by": v.get("LastModifiedBy", MISSING), "id": v.get("TxnID", MISSING)})
    cleared = sum_of(reconciled, lambda x: x["amount"])
    difference = round2(cleared - bank)
    changed = sorted((x for x in reconciled if x["date"] <= a["statement_date"] and x["modified"] is not MISSING and x["modified"] >= since),
                     key=lambda x: x["modified"], reverse=True)
    after = [x for x in reconciled if x["date"] > a["statement_date"]]
    deleted = []
    try:
        rs = ctx.qb.request("TxnDeletedQueryRq", {"TxnDelType": BANK_DELETES, "DeletedDateRangeFilter": {"FromDeletedDate": f"{since}T00:00:00"}})
        deleted = sorted(({"type": x.get("TxnDelType"), "ref": x.get("RefNumber", MISSING), "deleted": x.get("TimeDeleted")} for x in rets_of(rs)),
                         key=lambda x: text(x["deleted"]), reverse=True)
    except QbStatusError:
        pass

    def show(x: dict) -> dict:
        return {k: v for k, v in x.items() if v is not MISSING and v != ""}

    notes = []
    if not difference:
        notes.append(f"QuickBooks' reconciled total matches the bank's balance on {a['statement_date']}: the beginning balance is right. "
                     "If this statement still does not balance, look at its own items: qbd uncleared --account lists what is not cleared.")
    else:
        notes.append(f"QuickBooks counts {fmt(cleared)} as reconciled and the bank had {fmt(bank)}: {fmt(abs(difference))} "
                     f"{'more' if difference > 0 else 'less'} in QuickBooks."
                     + (" Look at the transactions below with the statement in hand." if changed or deleted else ""))
        notes.append("QuickBooks' own Reconciliation Discrepancy report (Reports > Banking) lists the reconciled transactions changed "
                     "since the last reconciliation, and the Previous Reconciliation report the ones cleared in it.")
        if "ModifiedTime" not in col:
            notes.append("QuickBooks sent the register without its Last modified column, so changed transactions cannot be listed here.")
        elif not changed and not deleted:
            notes.append("Nothing reconciled was changed or deleted since then. Intuit also names a missing opening balance, a file "
                         "converted from another QuickBooks version, and damaged data (qbd file-health, then Verify Data).")
    if after:
        notes.append(f"{len(after)} reconciled transaction(s) are dated after {a['statement_date']}; they count in the beginning balance too.")
    if any(x["amount"] == 0 for x in changed):
        notes.append("A reconciled transaction with amount 0 was voided after it was reconciled: that takes its amount out of the beginning balance.")
    return {
        "account": name,
        "statement_date": a["statement_date"],
        "bank_balance": bank,
        "reconciled_in_quickbooks": cleared,
        "difference": difference,
        "reconciled_transactions": len(reconciled),
        "changed_since": {"since": since, "transactions": [show(x) for x in changed[:a["limit"]]], **({"more": len(changed) - a["limit"]} if len(changed) > a["limit"] else {})},
        "deleted_since": [show(x) for x in deleted[:a["limit"]]],
        **({"deleted_note": "Deleted transactions keep no account or amount: check each one against the statement."} if deleted else {}),
        "notes": notes,
        **({"next_step": "Fix each changed, voided or deleted transaction back to what the bank shows, with the accountant if it is in a closed "
                         "period; the beginning balance follows. qbd changes " + since + " lists everything changed since."}
           if difference and (changed or deleted) else {}),
    }


# ---------------------------------------------------------------- qb_sales_tax_check

# Ways sales tax gets paid or moved around Pay Sales Tax (Intuit: "Always use the Pay Sales Tax window").
TAX_BYPASS = ["Check", "Bill", "CreditCardCharge", "JournalEntry"]


def account_row(r: Report, full_name: str) -> ReportRow | None:
    """An account's row in a summary report: its subtotal when it has subaccounts, written with or without its number.
    The full name is tried before its last part, so a subaccount is not taken for another account of the same name."""
    for name in dict.fromkeys([full_name, full_name.split(":")[-1]]):
        pattern = re.compile(rf"(total )?([^·]*·\s*)?{re.escape(name)}", re.I)
        rows = [x for x in r.rows if x.kind != "text" and pattern.fullmatch(x.label.strip())]
        if rows:
            return next((x for x in rows if x.kind == "subtotal"), rows[0])
    return None


@tool(
    "qb_sales_tax_check",
    "Why sales tax owed and the books disagree",
    "US sales tax: compares what QuickBooks' Sales Tax Liability report says is owed with the Sales Tax Payable balance "
    "on the balance sheet (accrual and cash) on a date, and lists what usually splits them: sales tax paid with Write "
    "Checks, a bill or a card charge instead of Pay Sales Tax, and journal entries to Sales Tax Payable with no vendor. "
    "Nothing is changed.",
    {
        "as_of": date("The date to compare on (default today)"),
        "from": date("List transactions dated on or after (default a year before as_of)"),
        "limit": Arg("int", "Most transactions listed", default=50, min=1, max=500),
    },
)
def sales_tax_check(ctx: Ctx, a: dict) -> dict:
    as_of = nz(a["as_of"], ctx.today())
    start = nz(a["from"], add_days(as_of, -365))
    if start > as_of:
        raise ToolError("from is after as_of.")
    names = [text(get(r, "FullName")) for r in ctx.qb.query("AccountQueryRq", {
        "AccountType": "OtherCurrentLiability", "IncludeRetElement": ["FullName", "SpecialAccountType"]}, 1000).rows
        if text(get(r, "SpecialAccountType")) == "SalesTaxPayable"]
    if not names:
        return {"as_of": as_of, "sales_tax_accounts": [], "notes": [
            "This company file has no Sales Tax Payable account: sales tax is off (Edit > Preferences > Sales Tax) or the "
            "file is not a US one. Canadian GST/HST and UK VAT are not covered."]}

    r = ctx.qb.report("GeneralDetailReportQueryRq", {
        "GeneralDetailReportType": "TxnDetailByAccount",
        "ReportPeriod": {"FromReportDate": start, "ToReportDate": as_of},
        "ReportAccountFilter": {"FullName": names},
        "ReportTxnTypeFilter": {"TxnTypeFilter": TAX_BYPASS},
        "IncludeColumn": ["TxnID", "TxnType", "Date", "RefNumber", "Name", "Memo", "Amount"],
    })
    col = {c.type: i for i, c in enumerate(r.columns)}
    paid: list[dict] = []
    journals: list[dict] = []
    for row in r.rows:
        v = {t: _at(row.values, i) for t, i in col.items()}
        if row.kind != "data" or not truthy(v.get("Date")):
            continue
        x = {k: y for k, y in {"date": text(v["Date"]), "type": v.get("TxnType", MISSING), "ref": v.get("RefNumber", MISSING),
                               "name": v.get("Name", MISSING), "memo": v.get("Memo", MISSING), "amount": num(v.get("Amount")),
                               "id": v.get("TxnID", MISSING)}.items() if y is not MISSING and y != ""}
        if "journal" not in text(x.get("type", "")).lower():
            paid.append(x)
        elif "name" not in x:
            journals.append(x)

    lr = ctx.qb.report("GeneralSummaryReportQueryRq", {"GeneralSummaryReportType": "SalesTaxLiability",
                                                       "ReportPeriod": {"FromReportDate": as_of, "ToReportDate": as_of}})
    i, total = col_index(lr, "payable"), find_row(lr, r"^total\Z")
    owed = round2(num(_at(total.values, i))) if total is not None and i >= 0 else MISSING
    books = {}
    for basis in ("Accrual", "Cash"):
        bs = ctx.qb.report("GeneralSummaryReportQueryRq", {"GeneralSummaryReportType": "BalanceSheetStandard",
                                                           "ReportPeriod": {"FromReportDate": as_of, "ToReportDate": as_of}, "ReportBasis": basis})
        books[basis.lower()] = round2(sum(num(last_number(account_row(bs, n))) for n in names))
    basis = text(lr.basis).lower() if text(lr.basis).lower() in books else "accrual"
    difference = round2(books[basis] - owed) if owed is not MISSING else MISSING

    notes = []
    if owed is MISSING:
        notes.append("QuickBooks' Sales Tax Liability report came without a TOTAL row in a Sales Tax Payable column: compare it "
                     "with the balance sheet by hand.")
    elif not difference:
        notes.append(f"The Sales Tax Liability report and the {basis} balance sheet agree: {fmt(owed)} owed on {as_of}.")
    elif lr.basis is MISSING and abs(books["cash"] - owed) < 0.005:
        notes.append("The Sales Tax Liability report agrees with the cash-basis balance sheet, not the accrual one: this file "
                     "likely owes sales tax when customers pay (Edit > Preferences > Sales Tax > Company Preferences, 'When do "
                     "you owe sales tax?'). Compare with a cash-basis balance sheet.")
    else:
        notes.append(f"The Sales Tax Liability report says {fmt(owed)} is owed on {as_of}; Sales Tax Payable on the {basis} balance "
                     f"sheet is {fmt(books[basis])}, {fmt(abs(difference))} {'more' if difference > 0 else 'less'}.")
        if not paid and not journals:
            notes.append(f"Nothing from {start} explains it: look further back with --from. Intuit also names a sales tax "
                         "discount whose vendor differs from the tax item's vendor, and damaged data (qbd file-health, then Verify Data).")
    if paid:
        notes.append("Sales tax paid with Write Checks, a bill or a card charge instead of Pay Sales Tax leaves Sales Tax Payable "
                     "wrong (Intuit). The fix: back up, enter each payment again in Pay Sales Tax with the same check number, then "
                     "delete the original, with the accountant if the period is closed.")
    if journals:
        notes.append("Journal entries to Sales Tax Payable need the tax agency in their Name column; QuickBooks warns 'You must "
                     "specify a vendor name for the sales tax account'.")
    return {
        "as_of": as_of,
        "sales_tax_accounts": names,
        "owed_per_liability_report": owed,
        **({"report_basis": lr.basis} if lr.basis is not MISSING else {}),
        "balance_sheet": books,
        "difference": difference,
        "paid_outside_pay_sales_tax": {"from": start, "transactions": paid[:a["limit"]], **({"more": len(paid) - a["limit"]} if len(paid) > a["limit"] else {})},
        "journal_entries_without_vendor": journals[:a["limit"]],
        "notes": notes,
    }


# ---------------------------------------------------------------- qb_currency

ASSET_TYPES = {"Bank", "AccountsReceivable", "OtherCurrentAsset", "FixedAsset", "OtherAsset"}


@tool(
    "qb_currency",
    "Foreign-currency balances explained",
    "Multicurrency files: QuickBooks' exchange rate for each foreign currency and the date it was set; each foreign "
    "account's balance in its currency, what the books carry it at in the home currency, what it is worth at the rate and "
    "the unrealized gain or loss, and the date of the last home currency adjustment; customers, vendors and accounts with "
    "nothing left in their currency but a home-currency "
    "balance still on them; and recent transactions whose exchange rate is far from the usual one, such as 1 or a rate "
    "typed upside down. Nothing is changed.",
    {
        "rates": Arg("list", "Value at these rates instead of QuickBooks' own: CODE=RATE in home currency per unit, e.g. USD=1.3712"),
        "days": Arg("int", "Check the exchange rates of transactions from this many days back", default=90, min=1, max=366),
        "max_rate_gap_percent": Arg("int", "List a transaction whose rate is at least this many percent from the usual one", default=10, min=1, max=90),
        "limit": Arg("int", "Most names and transactions listed per currency", default=20, min=1, max=200),
    },
)
def currency(ctx: Ctx, a: dict) -> dict:
    given: dict[str, float] = {}
    for x in nz(a["rates"], []):
        code, _, value = x.partition("=")
        rate = to_number(value)
        if not code.strip() or not finite(rate) or rate <= 0:
            raise ToolError(f'rates: "{x}" is not CODE=RATE, such as USD=1.3712.')
        given[code.strip().upper()] = rate
    prefs = get(ctx.qb.request("PreferencesQueryRq", {"IncludeRetElement": ["MultiCurrencyPreferences"]}).body.get("PreferencesRet", MISSING), "MultiCurrencyPreferences")
    if not truthy(get(prefs, "IsMultiCurrencyOn")):
        return {"multi_currency": False, "notes": ["Multicurrency is off in this company file, so every amount is in one currency."]}
    home = text(ref_name(get(prefs, "HomeCurrencyRef")))

    def foreign(r: Any) -> str:
        cur = text(nz(ref_name(get(r, "CurrencyRef")), home))
        return "" if cur.lower() == home.lower() else cur

    accounts = [r for r in ctx.qb.query("AccountQueryRq", {"ActiveStatus": "All", "IncludeRetElement": ["FullName", "AccountType", "TotalBalance", "CurrencyRef"]}, 3000).rows if foreign(r)]
    names = {text(get(r, "FullName")) for r in accounts}
    # A subaccount is counted in its parent's total.
    accounts = [r for r in accounts if not any(text(get(r, "FullName")).startswith(f"{n}:") for n in names)]
    if not accounts:
        return {"multi_currency": True, "home_currency": home, "currencies": [], "notes": [f"No account holds a currency other than {home} yet."]}
    rates = {text(get(r, "Name")).lower(): r for r in ctx.qb.query("CurrencyQueryRq", {"ActiveStatus": "All", "IncludeRetElement": ["Name", "CurrencyCode", "ExchangeRate", "AsOfDate"]}, 1000).rows}
    # All dates, like the accounts' own balances, which include transactions dated ahead.
    bs = ctx.qb.report("GeneralSummaryReportQueryRq", {"GeneralSummaryReportType": "BalanceSheetStandard", "ReportDateMacro": "All", "ReportBasis": "Accrual"})
    out: dict[str, dict] = {}
    for r in accounts:
        cur, name, kind = foreign(r), text(get(r, "FullName")), text(get(r, "AccountType"))
        if cur not in out:
            q = rates.get(cur.lower(), {})
            code = text(nz(get(q, "CurrencyCode"), cur)).upper()
            out[cur] = {"currency": cur, "code": code, "rate": given.get(code, num(get(q, "ExchangeRate"))) or MISSING,
                        "rate_from": "given" if code in given else "QuickBooks",
                        **({"rate_set_on": text(q["AsOfDate"])} if code not in given and truthy(get(q, "AsOfDate")) else {}),
                        "gain": MISSING, "accounts": [], "left_over": [], "transactions_checked": 0, "usual_rate": MISSING, "unusual_rates": []}
        x = out[cur]
        balance, carrying = num(get(r, "TotalBalance")), round2(num(last_number(account_row(bs, name))))
        if not balance and not carrying:
            continue
        row = {"account": name, "type": kind, "balance": balance, "carrying_value": carrying}
        if x["rate"] is not MISSING:
            worth = round2(balance * x["rate"])
            row |= {"at_rate": worth, "gain": round2((worth - carrying) * (1 if kind in ASSET_TYPES else -1))}
        x["accounts"].append(row)
        # Receivables and payables are checked customer by customer and vendor by vendor below.
        if not balance and kind not in ("AccountsReceivable", "AccountsPayable"):
            x["left_over"].append({"kind": "account", "name": name, "carrying_value": carrying})

    in_use = sorted(out)
    # A vendor has a Name but no FullName.
    for query, field, report, kind in (("CustomerQueryRq", "FullName", "CustomerBalanceSummary", "customer"),
                                       ("VendorQueryRq", "Name", "VendorBalanceSummary", "vendor")):
        settled = {text(get(r, field)).lower(): r for r in ctx.qb.query(query, {
            "CurrencyFilter": {"FullName": in_use}, "IncludeRetElement": [field, "Balance", "CurrencyRef"]}, CAP).rows
            if not num(get(r, "Balance")) and foreign(r) in out}
        if not settled:
            continue
        # A name's own row: its balance without its jobs, as Balance is.
        for row in ctx.qb.report("GeneralSummaryReportQueryRq", {"GeneralSummaryReportType": report, "ReportDateMacro": "All"}).rows:
            if row.kind == "data" and (r := settled.pop(row.label.strip().lower(), None)) and (value := round2(num(last_number(row)))):
                out[foreign(r)]["left_over"].append({"kind": kind, "name": text(get(r, field)), "carrying_value": value})

    since = add_days(ctx.today(), -a["days"])
    seen: dict[str, list[dict]] = {}
    for t in ctx.qb.query("TransactionQueryRq", {
        "TransactionDateRangeFilter": {"FromTxnDate": since, "ToTxnDate": ctx.today()}, "CurrencyFilter": {"FullName": in_use},
        "IncludeRetElement": ["TxnType", "TxnID", "TxnDate", "RefNumber", "EntityRef", "CurrencyRef", "ExchangeRate", "Amount", "AmountInHomeCurrency"],
    }, CAP).rows:
        if finite(get(t, "ExchangeRate")) and t["ExchangeRate"] > 0 and foreign(t) in out:
            seen.setdefault(foreign(t), []).append(t)
    for cur, txns in seen.items():
        x = out[cur]
        # The median of the period's rates other than 1, which usually means no rate was set; with few of them, the rate
        # the balances are valued at.
        set_rates = [t["ExchangeRate"] for t in txns if t["ExchangeRate"] != 1]
        usual = statistics.median(set_rates) if len(set_rates) >= 5 else x["rate"]
        if usual is MISSING:
            continue
        x["transactions_checked"], x["usual_rate"] = len(txns), usual
        for t in txns:
            rate = t["ExchangeRate"]
            if abs(rate / usual - 1) * 100 < a["max_rate_gap_percent"]:
                continue
            amount = num(get(t, "Amount"))
            home_amount = num(nz(get(t, "AmountInHomeCurrency"), round2(amount * rate)))
            hint = "rate_is_1" if rate == 1 else "upside_down" if abs(rate * usual - 1) <= 0.05 else MISSING
            x["unusual_rates"].append({k: v for k, v in {
                "type": get(t, "TxnType"), "ref": get(t, "RefNumber"), "date": text(get(t, "TxnDate")), "name": ref_name(get(t, "EntityRef")),
                "rate": rate, "amount": amount, "home_amount": home_amount, "at_usual_rate": round2(amount * usual), "hint": hint, "id": get(t, "TxnID"),
            }.items() if v is not MISSING})
        x["unusual_rates"].sort(key=lambda u: -abs(u["at_usual_rate"] - u["home_amount"]))

    # QuickBooks marks the journal entries a home currency adjustment makes.
    adjustments = [j for j in ctx.qb.query("JournalEntryQueryRq", {
        "TxnDateRangeFilter": {"FromTxnDate": add_days(ctx.today(), -400), "ToTxnDate": ctx.today()},
        "IncludeRetElement": ["TxnDate", "RefNumber", "IsHomeCurrencyAdjustment"],
    }, CAP).rows if get(j, "IsHomeCurrencyAdjustment") is True]
    last = max(adjustments, key=lambda j: text(get(j, "TxnDate")), default=None)

    notes = []
    for x in out.values():
        if x["rate"] is MISSING:
            notes.append(f"QuickBooks has no {x['code']} rate: pass --rates {x['code']}=<rate> to value the {x['code']} balances.")
        elif "rate_set_on" in x and (age := days_between(x["rate_set_on"], ctx.today())) > 30:
            # Intuit: QuickBooks downloads rates only when the home currency is the US dollar.
            update = ("Lists > Currency List > Activities > Download Latest Exchange Rates" if text(get(rates.get(home.lower(), {}), "CurrencyCode")) == "USD"
                      else "Lists > Currency List, open the currency and enter the rate for the date")
            notes.append(f"QuickBooks' {x['code']} rate ({x['rate']}) was set on {x['rate_set_on']}, {age} days ago. New {x['code']} transactions "
                         f"start from it, and the values here use it: update it in QuickBooks ({update}) or pass --rates {x['code']}=<rate>.")
        if x["rate"] is not MISSING:
            x["gain"] = sum_of(x["accounts"], lambda r: r["gain"])
    gain = round2(sum(x["gain"] for x in out.values() if x["gain"] is not MISSING))
    if gain:
        notes.append(f"At these rates the foreign balances show an unrealized exchange {'gain' if gain > 0 else 'loss'} of {fmt(abs(gain))} "
                     f"{home} against what the books carry. A home currency adjustment books it (Company > Manage "
                     "Currency > Home Currency Adjustment), made by the accountant, usually at a month end with that day's rate; "
                     + (f"the last one is dated {text(last['TxnDate'])}." if last else "there is none in the last 400 days."))
    if any(x["left_over"] for x in out.values()):
        notes.append(f"Names and accounts under left_over have nothing left in their currency but still carry {home}. A home currency "
                     "adjustment ignores foreign balances of zero, so each stays until the accountant clears it with a journal entry "
                     "marked as a home currency adjustment, against Exchange Gain or Loss.")
    if any(x["unusual_rates"] for x in out.values()):
        notes.append(f"Transactions under unusual_rates since {since} use a rate at least {a['max_rate_gap_percent']}% from the usual one, "
                     "so their home amounts and the books follow that rate. rate_is_1 usually means no rate was set; upside_down means "
                     "it was entered the other way round, as units of the currency per home unit. Fixing one means editing its rate in "
                     "QuickBooks, which changes its home amount: in a closed period, only with the accountant.")
    if unused := sorted(set(given) - {x["code"] for x in out.values()}):
        notes.append(f"No foreign account holds {', '.join(unused)}, so those --rates were not used.")

    def capped(x: dict, key: str) -> dict:
        return {key: x[key][:a["limit"]], **({f"{key}_more": len(x[key]) - a["limit"]} if len(x[key]) > a["limit"] else {})}

    currencies = [{**x, **capped(x, "left_over"), **capped(x, "unusual_rates")} for x in out.values()]
    return {"multi_currency": True, "home_currency": home, "currencies": currencies,
            "last_adjustment": {"date": text(last["TxnDate"]), "ref": get(last, "RefNumber")} if last else MISSING, "notes": notes}


# ---------------------------------------------------------------- qb_find_duplicates

SUFFIXES = re.compile(r"\b(the|inc|incorporated|llc|l\.l\.c|ltd|limited|co|corp|corporation|company|plc|pty|gmbh)\b", re.ASCII)


def normalize_name(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = "".join(ch if ch == " " or unicodedata.category(ch)[0] in "LN" else " " for ch in s)
    return re.sub(r"\s+", " ", SUFFIXES.sub(" ", s)).strip()


@tool(
    "qb_find_duplicates",
    "Find duplicate entries",
    "Look for bills entered twice, bills also paid by a separate check or card charge, invoices billed twice, repeated "
    "expenses, and customers or vendors that exist under two names. Each finding has a confidence and the records involved. "
    "Nothing is changed.",
    {
        "from": date("Transactions dated on or after (default 90 days ago)"),
        "to": date("Transactions dated on or before (default today)"),
        "window_days": Arg("int", "Same amount within this many days counts as a possible duplicate", default=7, min=0, max=31),
        "include": Arg("list", default=["bills", "expenses", "invoices", "names"], choices=("bills", "expenses", "invoices", "names")),
    },
)
def find_duplicates(ctx: Ctx, a: dict) -> dict:
    to = nz(a["to"], ctx.today())
    from_ = nz(a["from"], add_days(to, -90))
    include = a["include"]
    findings: list[dict] = []
    notes: list[str] = []

    def near(x: dict, y: dict) -> bool:
        return abs(days_between(x["date"], y["date"])) <= a["window_days"]

    def show(m: dict) -> dict:
        return {"type": m["type"], "txn_id": m["id"], "ref": m["ref"], "date": m["date"], "party": m["party"], "amount": m["amount"]}

    def party(m: dict) -> str:
        return m["party"].lower()

    def same_amount(x: dict, y: dict) -> bool:
        return party(x) == party(y) and x["amount"] == y["amount"] and x["amount"] != 0 and near(x, y)

    def apart(x: dict, y: dict) -> str:
        return f"{fmt(x['amount'])}, {abs(days_between(x['date'], y['date']))} day(s) apart"

    def pairs(xs: list[dict], kind: str, confidence: str, reason: Callable[[dict, dict], str], same: Callable[[dict, dict], bool]) -> None:
        ordered = sorted(xs, key=lambda p: p["date"])
        seen: set[str] = set()
        for i, x in enumerate(ordered):
            for y in ordered[i + 1:]:
                if days_between(x["date"], y["date"]) > max(a["window_days"], 31):
                    break
                if x["id"] == y["id"] or not same(x, y):
                    continue
                key = "|".join(sorted([x["id"], y["id"]]))
                if key in seen:
                    continue
                seen.add(key)
                findings.append({"kind": kind, "confidence": confidence, "reason": reason(x, y), "records": [show(x), show(y)]})

    # One summary query for all four types: on a big file it is several times quicker than reading the records.
    types = {"Bill": "bill", "Check": "check", "CreditCardCharge": "credit_card_charge", "Invoice": "invoice"}
    wanted = [t for t in types if ("invoices" in include if t == "Invoice" else "bills" in include or "expenses" in include)]
    money: list[dict] = []
    if wanted:
        res = ctx.qb.query("TransactionQueryRq", {
            "TransactionDateRangeFilter": {"FromTxnDate": from_, "ToTxnDate": to}, "TransactionTypeFilter": {"TxnTypeFilter": wanted},
            "TransactionDetailLevelFilter": "SummaryOnly", "IncludeRetElement": ["TxnType", "TxnID", "TxnDate", "RefNumber", "EntityRef", "Amount"],
        }, CAP * 4)
        if res.truncated:
            notes.append(f"Only the first {CAP * 4} transactions were checked; narrow the dates.")
        # QuickBooks signs amounts by their effect on the books (bills come back negative); only the size matters here.
        money = [
            {
                "type": types.get(text(get(r, "TxnType")), MISSING), "id": text(get(r, "TxnID")), "ref": get(r, "RefNumber"), "date": text(get(r, "TxnDate")),
                "party": nz(ref_name(get(r, "EntityRef")), "?"), "amount": abs(num(get(r, "Amount"))),
            }
            for r in res.rows
        ]
    bills = [m for m in money if m["type"] == "bill"]
    spend = [m for m in money if m["type"] in ("check", "credit_card_charge")]
    if "bills" in include:
        by_ref: dict[str, list[dict]] = {}
        for b in bills:
            if truthy(b["ref"]):
                by_ref.setdefault(f"{party(b)}|{text(b['ref']).lower()}", []).append(b)
        same_ref: set[str] = set()
        for group in by_ref.values():
            if len(group) < 2:
                continue
            same_ref.update(b["id"] for b in group)
            findings.append({"kind": "bill entered twice", "confidence": "high", "reason": f"Same vendor and same bill number {text(group[0]['ref'])}", "records": [show(b) for b in group]})
        pairs([b for b in bills if b["id"] not in same_ref], "possible duplicate bill", "medium", lambda x, y: f"Same vendor and amount {apart(x, y)}", same_amount)
        pairs(
            [*bills, *spend], "bill also paid directly", "high",
            lambda x, y: f"A bill and a {y['type'] if x['type'] == 'bill' else x['type']} to the same vendor for {fmt(x['amount'])}: the bill was paid twice, or the payment is not linked to it",
            lambda x, y: (x["type"] == "bill") != (y["type"] == "bill") and same_amount(x, y),
        )
    if "expenses" in include:
        pairs(spend, "possible duplicate expense", "medium", lambda x, y: f"Same payee and amount {apart(x, y)}", same_amount)
    if "invoices" in include:
        pairs([m for m in money if m["type"] == "invoice"], "possible double billing", "medium", lambda x, y: f"Same customer and amount {apart(x, y)}", same_amount)
    if "names" in include:
        reported: set[str] = set()
        for request, kind in (("CustomerQueryRq", "customer"), ("VendorQueryRq", "vendor")):
            res = ctx.qb.query(request, {"ActiveStatus": "ActiveOnly", "IncludeRetElement": ["ListID", "FullName" if kind == "customer" else "Name", "Email", "Phone", "Balance"]}, CAP * 2, page_size=500)
            people = [
                {
                    "id": text(get(r, "ListID")), "name": text(nz(get(r, "FullName"), get(r, "Name"))), "email": text(nz(get(r, "Email"), "")).lower(),
                    "balance": num(get(r, "Balance")),
                }
                for r in res.rows
            ]

            def group_by(key: Callable[[dict], str], reason: str, confidence: str) -> None:
                groups: dict[str, list[dict]] = {}
                for p in people:
                    k = key(p)
                    if k:
                        groups.setdefault(k, []).append(p)
                for group in groups.values():
                    # Jobs of one customer share its email; only different customers count.
                    ids = "|".join(sorted(p["id"] for p in group))
                    if len({p["name"].split(":")[0] for p in group}) > 1 and ids not in reported:
                        reported.add(ids)
                        findings.append({
                            "kind": f"{kind} listed twice", "confidence": confidence, "reason": reason,
                            "records": [{"type": kind, "list_id": p["id"], "name": p["name"], "balance": p["balance"]} for p in group],
                        })

            group_by(lambda p: "" if ":" in p["name"] else normalize_name(p["name"]), "Names match once punctuation and words like Inc or LLC are ignored", "high")
            group_by(lambda p: p["email"], "Different names share an email address", "medium")
    findings.sort(key=lambda f: 0 if f["confidence"] == "high" else 1)
    return {
        "checked": {"from": from_, "to": to, "window_days": a["window_days"]},
        "found": len(findings),
        "findings": findings[:50],
        **({"more": len(findings) - 50} if len(findings) > 50 else {}),
        **({"notes": notes} if notes else {}),
        "next_step": "Open each pair in QuickBooks before changing anything; a matching amount is not proof of a duplicate.",
    }


# ---------------------------------------------------------------- qb_health_check


@tool(
    "qb_health_check",
    "Books health check (month-end readiness)",
    "Checks the company file for what an accountant fixes before closing a month: closing date, uncategorized amounts, "
    "Opening Balance Equity, undeposited payments, unapplied customer payments and vendor credits, very old receivables "
    "and payables, negative stock, sales orders and purchase orders left open, and future-dated transactions. Read-only.",
    {"period_end": date("Month end being closed (default: end of last month)")},
)
def health_check(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    period_end = nz(a["period_end"], month_end(today, -1))
    facts = company_facts(ctx)
    checks: list[dict] = []

    def check(id_: str, title: str, fn: Callable[[], dict]) -> None:
        try:
            checks.append({"check": id_, "title": title, **fn()})
        except Exception as e:
            checks.append({"check": id_, "title": title, "status": "error", "finding": message(e)})

    def closing_date() -> dict:
        closing = facts.closing_date
        if not closing:
            return {"status": "warn", "finding": "No closing date is set, so past periods can still be changed.", "fix": "In QuickBooks: Edit > Preferences > Accounting > Company Preferences > Set Date/Password."}
        if closing < add_days(period_end, -62):
            return {"status": "warn", "finding": f"Books are closed through {closing}, more than a month before {period_end}.", "fix": "Move the closing date forward once the period is reviewed."}
        return {"status": "ok", "finding": f"Books are closed through {closing}."}

    def suspense_accounts() -> dict:
        bad = [
            x for x in account_list(ctx)
            if re.search(r"opening balance equity|uncategori[sz]ed|ask my accountant|suspense", x["name"], re.I) and x["balance"] != 0
            and x["type"] not in ("Income", "Expense", "OtherIncome", "OtherExpense", "CostOfGoodsSold")
        ]
        pl = profit_and_loss(ctx, fiscal_year_start(period_end, facts.first_fiscal_month), period_end)
        pl_bad = [
            {"account": r.label.strip(), "year_to_date": last_number(r)}
            for r in pl.rows
            if r.kind == "data" and re.search(r"uncategori[sz]ed|ask my accountant|suspense", r.label, re.I) and nz(last_number(r), 0) != 0
        ]
        items = [*({"account": x["name"], "balance": x["balance"]} for x in bad), *pl_bad]
        if items:
            return {"status": "warn", "finding": f"{len(items)} account(s) hold amounts that still need a real category.", "details": items, "fix": "Reclassify these amounts to the correct accounts (with your accountant for Opening Balance Equity)."}
        return {"status": "ok", "finding": "No uncategorized amounts and Opening Balance Equity is zero."}

    def undeposited_funds() -> dict:
        rs = ctx.qb.request("ReceivePaymentToDepositQueryRq", {})
        rows = [
            {"customer": ref_name(get(r, "CustomerRef")), "date": text(get(r, "TxnDate")), "ref": get(r, "RefNumber"), "amount": num(get(r, "Amount")), "days": days_between(text(get(r, "TxnDate")), today)}
            for r in nz(rs.body.get("ReceivePaymentToDepositRet", MISSING), [])
        ]
        old = sorted((r for r in rows if r["days"] > 7), key=lambda r: r["days"], reverse=True)
        if not old:
            return {"status": "ok", "finding": f"{len(rows)} recent payment(s) waiting to be deposited." if rows else "Undeposited Funds is clear."}
        return {
            "status": "warn", "finding": f"{len(old)} payment(s) older than 7 days are still in Undeposited Funds ({fmt(sum_of(old, lambda r: r['amount']))}).",
            "details": old[:10], "fix": "Record the bank deposits (Banking > Make Deposits) matching your bank statement.",
        }

    # Both agings are read once, by the first check that needs them.
    aging: dict[str, list[dict]] = {}

    def parties(side: str) -> list[dict]:
        if side not in aging:
            aging[side] = by_party(open_items(ctx, side))
        return aging[side]

    def unapplied(c: dict) -> dict:
        return {"type": c["type"], "date": c["date"], "ref": c["ref"], "unapplied": -c["open"], "txn_id": c["id"]}

    def unapplied_payments() -> dict:
        rows = [{"customer": c["party"], **unapplied(c)} for c in sorted((c for p in parties("AR") for c in p["credits"]), key=lambda c: c["open"])]
        if rows:
            return {
                "status": "warn", "finding": f"{len(rows)} payment(s) and credit(s) have {fmt(sum_of(rows, lambda r: r['unapplied']))} not applied to any invoice.",
                "details": rows[:10], "fix": "Apply them to the customers' open invoices, or refund.",
            }
        return {"status": "ok", "finding": "Every customer payment and credit is applied."}

    def old_receivables() -> dict:
        old = sorted((d for p in parties("AR") for d in p["docs"] if d["left"] > 0 and days_between(d["due"], today) > 90), key=lambda d: d["left"], reverse=True)
        if old:
            return {
                "status": "warn", "finding": f"{len(old)} invoice(s) are more than 90 days late ({fmt(sum_of(old, lambda d: d['left']))}).",
                "details": [
                    {"customer": d["party"], **({} if re.search("invoice", d["type"], re.I) else {"type": d["type"]}), "ref": d["ref"], "due": d["due"], "balance": d["left"]}
                    for d in old[:10]
                ],
                "fix": "Chase them (qbd receivables) or, with your accountant, write off what will not be collected.",
            }
        return {"status": "ok", "finding": "No invoice is more than 90 days late."}

    def old_payables() -> dict:
        old = sorted((d for p in parties("AP") for d in p["docs"] if d["left"] > 0 and days_between(d["due"], today) > 60), key=lambda d: d["left"], reverse=True)
        if old:
            return {
                "status": "warn",
                "finding": f"{len(old)} bill(s) are more than 60 days late ({fmt(sum_of(old, lambda d: d['left']))}). Some may have been paid by a check that is not linked to the bill.",
                "details": [
                    {"vendor": d["party"], **({} if d["type"] == "Bill" else {"type": d["type"]}), "ref": d["ref"], "due": d["due"], "open": d["left"]}
                    for d in old[:10]
                ],
                "fix": "Pay them, or link the payment that was recorded as a check (see qbd find-duplicates).",
            }
        return {"status": "ok", "finding": "No bill is more than 60 days late."}

    def unapplied_vendor_credits() -> dict:
        rows = [{"vendor": c["party"], **unapplied(c)} for c in sorted((c for p in parties("AP") for c in p["credits"]), key=lambda c: c["open"])]
        if rows:
            return {
                "status": "warn", "finding": f"{len(rows)} vendor credit(s) and payment(s) have {fmt(sum_of(rows, lambda r: r['unapplied']))} not applied to any bill.",
                "details": rows[:10], "fix": "Apply them in Pay Bills when paying the vendor's next bills, or ask the vendor for a refund.",
            }
        return {"status": "ok", "finding": "Every vendor credit and payment is applied."}

    def negative_inventory() -> dict:
        res = ctx.qb.query("ItemInventoryQueryRq", {"ActiveStatus": "ActiveOnly", "IncludeRetElement": ["FullName", "QuantityOnHand"]}, CAP * 2, page_size=500)
        neg = [{"item": get(r, "FullName"), "on_hand": num(get(r, "QuantityOnHand"))} for r in res.rows if num(get(r, "QuantityOnHand")) < 0]
        if neg:
            return {
                "status": "warn", "finding": f"{len(neg)} item(s) have negative quantity on hand, which distorts cost of goods sold.",
                "details": neg[:15], "fix": "qbd stock-history <item> shows what took each one below zero; then enter the missing purchases or receipts, or count and adjust the stock.",
            }
        return {"status": "ok", "finding": "No item has negative stock."}

    def stale_documents() -> dict:
        # QuickBooks' Open Sales Orders and Open Purchase Orders reports take under a second; reading the orders
        # themselves to find the open ones took minutes on a big file.
        def open_orders(report: str, type_: str) -> list[dict]:
            r = ctx.qb.report("GeneralDetailReportQueryRq", {
                "GeneralDetailReportType": report, "ReportPeriod": {"FromReportDate": add_days(today, -3650), "ToReportDate": add_days(today, -90)},
                "ReportDetailLevelFilter": "SummaryOnly", "IncludeColumn": ["TxnID", "TxnType", "Date", "RefNumber", "Name", "Amount"],
            })
            at = {"date": column(r, r"^date\Z", "Date"), "ref": column(r, r"^num\Z", "RefNumber"), "name": column(r, r"^name\Z", "Name"), "amount": column(r, r"^amount\Z", "Amount")}
            # abs: the Open Purchase Orders report gives each order as a negative amount.
            rows = [
                {"type": type_, "ref": _at(x.values, at["ref"]), "date": text(nz(_at(x.values, at["date"]), "")), "party": _at(x.values, at["name"]), "amount": abs(num(_at(x.values, at["amount"])))}
                for x in r.rows if x.kind == "data"
            ]
            return sorted(rows, key=lambda x: x["date"])

        so = open_orders("OpenSalesOrderByCustomer", "sales_order")
        po = open_orders("OpenPOs", "purchase_order")
        if so or po:
            return {
                "status": "info", "finding": f"{len(so)} sales order(s) and {len(po)} purchase order(s) are open and older than 90 days.",
                "details": [*so[:5], *po[:5]], "fix": "Close what will not happen, so open-order and on-order figures stay true.",
            }
        return {"status": "ok", "finding": "No sales order or purchase order has been open for more than 90 days."}

    def future_dated() -> dict:
        res = ctx.qb.query("TransactionQueryRq", {
            "TransactionDateRangeFilter": {"FromTxnDate": add_days(today, 8), "ToTxnDate": add_days(today, 3650)},
            "IncludeRetElement": ["TxnType", "TxnID", "TxnDate", "RefNumber", "EntityRef", "Amount"],
        }, 200)
        rows = [
            {"type": get(r, "TxnType"), "date": get(r, "TxnDate"), "ref": get(r, "RefNumber"), "party": ref_name(get(r, "EntityRef")), "amount": get(r, "Amount")}
            for r in res.rows
        ]
        if rows:
            return {
                "status": "warn", "finding": f"{len(rows)}{'+' if res.truncated else ''} transaction(s) are dated more than a week ahead; usually a typo in the date.",
                "details": rows[:10], "fix": "Correct the dates.",
            }
        return {"status": "ok", "finding": "No transaction is dated more than a week ahead."}

    check("closing_date", "Closing date set", closing_date)
    check("suspense_accounts", "Opening Balance Equity and uncategorized balances", suspense_accounts)
    check("undeposited_funds", "Payments waiting to be deposited", undeposited_funds)
    check("unapplied_payments", "Customer payments and credits not applied to invoices", unapplied_payments)
    check("old_receivables", "Receivables more than 90 days late", old_receivables)
    check("old_payables", "Bills more than 60 days late", old_payables)
    check("unapplied_vendor_credits", "Vendor credits and payments not applied to bills", unapplied_vendor_credits)
    if facts.uses_inventory:
        check("negative_inventory", "Negative stock", negative_inventory)
    check("stale_documents", "Old open sales orders and purchase orders", stale_documents)
    check("future_dated", "Transactions dated in the future", future_dated)

    def count(s: str) -> int:
        return sum(1 for c in checks if c["status"] == s)

    return {
        "period_end": period_end,
        "company": facts.name,
        "summary": {"ok": count("ok"), "warnings": count("warn"), "info": count("info"), "errors": count("error")},
        "checks": checks,
    }


# ---------------------------------------------------------------- qb_compare_periods


def _lines(r: Report) -> dict[str, dict]:
    """Each non-text row by its path of section titles, e.g. "Income > Merchandise Sales"."""
    out: dict[str, dict] = {}
    path: list[Any] = []
    for row in r.rows:
        path = (path + [None] * row.depth)[:row.depth]
        if row.kind == "text":
            path.append(row.label)
            continue
        out[" > ".join([*(p for p in path if p), row.label])] = {"label": row.label, "kind": row.kind, "value": nz(last_number(row), 0)}
    return out


@tool(
    "qb_compare_periods",
    "Compare profit and loss between periods",
    "Profit and loss for two periods side by side (default: last month against the month before), with the lines that "
    "changed most. Use it to explain why profit moved.",
    {
        "current_from": date("Current period start"),
        "current_to": date("Current period end"),
        "previous_from": date("Previous period start"),
        "previous_to": date("Previous period end"),
        "basis": Arg("enum", choices=("Accrual", "Cash")),
        "top": Arg("int", default=12, min=1, max=50),
    },
)
def compare_periods(ctx: Ctx, a: dict) -> dict:
    today = ctx.today()
    cur = {"from": nz(a["current_from"], month_start(today, -1)), "to": nz(a["current_to"], month_end(today, -1))}
    prev = {"from": nz(a["previous_from"], month_start(cur["from"], -1)), "to": nz(a["previous_to"], month_end(cur["from"], -1))}
    rc = profit_and_loss(ctx, cur["from"], cur["to"], a["basis"])
    rp = profit_and_loss(ctx, prev["from"], prev["to"], a["basis"])
    lc, lp = _lines(rc), _lines(rp)
    rows = []
    for k in dict.fromkeys([*lc, *lp]):
        c = lc[k]["value"] if k in lc else 0
        p = lp[k]["value"] if k in lp else 0
        rows.append({"line": k, "kind": nz(get(lc.get(k), "kind"), get(lp.get(k), "kind")), "current": c, "previous": p, "change": round2(c - p), "change_pct": pct(c, p)})

    def key(pattern: str) -> Any:
        return next((r for r in rows if re.search(pattern, r["line"].split(" > ")[-1], re.I)), MISSING)

    return {
        "current": cur,
        "previous": prev,
        "basis": rc.basis,
        "key_lines": [x for x in (key(r"^total income\Z"), key(r"^gross profit\Z"), key(r"^total expenses?\Z"), key(r"^net income\Z")) if x is not MISSING],
        "biggest_changes": sorted((r for r in rows if r["kind"] == "data" and r["change"] != 0), key=lambda r: abs(r["change"]), reverse=True)[:a["top"]],
    }


INSIGHT_TOOLS = [daily_brief, trace, receivables, match_payment, deposits, payables, coding, cash_position, inventory_reorder, stock, backorders, purchasing, stock_history, form_1099, uncleared, reconcile_check, price_check, sales_tax_check, currency, find_duplicates, health_check, compare_periods]
