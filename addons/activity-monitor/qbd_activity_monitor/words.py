"""Everything the Activity Monitor says, in plain English: QuickBooks requests and reports, problems with what to do
about them, arguments with personal data masked, times, numbers and amounts. Nothing here reads a file."""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

from qbd_agent_kit.api import HINTS, MISSING, STATUS_HINTS


# Set from the Windows region by the window; plain Python formatting otherwise.
GROUP, DECIMAL, CLOCK_12 = ",", ".", False

# ---------------------------------------------------------------- numbers, amounts, times


def num(n: Any) -> str:
    """1,200 (thousands separated, as the Windows region writes them)."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n):
        return str(n)
    s = f"{n:,}" if float(n).is_integer() else f"{n:,.2f}"
    return s.replace(",", "\0").replace(".", DECIMAL).replace("\0", GROUP)


def money(n: Any) -> str:
    """3,231.95: amounts always show their cents."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n):
        return str(n)
    return f"{n:,.2f}".replace(",", "\0").replace(".", DECIMAL).replace("\0", GROUP)


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{num(n)} {one if n == 1 else many or one + 's'}"


def secs(ms: Any, fine: bool = False) -> str:
    """How long something took: 0.4 s, 18 s, 2 min 5 s; fine: 0.09 s under a second, for QuickBooks steps."""
    s = (ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else 0) / 1000
    if fine and s < 1:
        return f"{max(s, 0.01):.2f} s".replace(".", DECIMAL)
    if s < 0.05:
        return "0.0 s" if s <= 0 else "under 0.1 s"
    if s < 10:
        return f"{s:.1f} s".replace(".", DECIMAL)
    if s < 60:
        return f"{round(s)} s"
    m, r = divmod(round(s), 60)
    if m < 60:
        return f"{m} min" + (f" {r} s" if r else "")
    h, m = divmod(m, 60)
    return f"{h} h" + (f" {m} min" if m else "")


def span(seconds: float) -> str:
    """A length of time in words: 12 s, 3 min, 2 h 10 min, 3 days."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h" + (f" {m} min" if m else "")
    return plural(s // 86400, "day")


def local(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t).astimezone()


def clock(t: float, seconds: bool = False) -> str:
    """08:31, or 08:31:12; 8:31 AM when the region uses a 12-hour clock."""
    d = local(t)
    if CLOCK_12:
        return f"{d.hour % 12 or 12}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "") + (" PM" if d.hour >= 12 else " AM")
    return f"{d.hour:02d}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "")


# English names whatever the PC's language, as every other word here
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def weekday(d: dt.date, short: bool = False) -> str:
    return DAYS[d.weekday()][:3] if short else DAYS[d.weekday()]


def month(d: dt.date, short: bool = False) -> str:
    return MONTHS[d.month - 1][:3] if short else MONTHS[d.month - 1]


def day(t: float, now: float) -> str:
    """Today, Yesterday, Monday 19 October; with the year when it is not this year."""
    d, n = local(t).date(), local(now).date()
    if d == n:
        return "Today"
    if d == n - dt.timedelta(days=1):
        return "Yesterday"
    return f"{weekday(d)} {d.day} {month(d)}" + (f" {d.year}" if d.year != n.year else "")


def short_day(t: float, now: float) -> str:
    """Today, Yesterday, Mon 19 Oct."""
    d, n = local(t).date(), local(now).date()
    if d == n:
        return "Today"
    if d == n - dt.timedelta(days=1):
        return "Yesterday"
    return f"{weekday(d, True)} {d.day} {month(d, True)}" + (f" {d.year}" if d.year != n.year else "")


def when(t: float, now: float) -> str:
    """09:44 today, Yesterday 16:40, Mon 19 Oct 14:05."""
    return clock(t) if short_day(t, now) == "Today" else f"{short_day(t, now)} {clock(t)}"


def exact(t: float) -> str:
    """The full local time, for hover and keyboard focus: Wednesday 21 October 2026, 08:31:49."""
    d = local(t)
    return f"{weekday(d)} {d.day} {month(d)} {d.year}, {clock(t, seconds=True)}"


def ago(t: float, now: float) -> str:
    """just now, 4 min ago, 2 h ago, yesterday at 17:42, Mon 19 Oct at 14:05."""
    s = now - t
    if s < 45:
        return "just now"
    if s < 3600:
        return f"{max(1, round(s / 60))} min ago"
    if short_day(t, now) == "Today":
        return f"{int(s // 3600)} h ago"
    return f"{short_day(t, now).lower() if short_day(t, now) == 'Yesterday' else short_day(t, now)} at {clock(t)}"


def iso_time(iso: Any) -> float | None:
    """The epoch time of a log time such as 2026-10-21T15:31:49.402Z."""
    try:
        return dt.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        try:
            return dt.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            return None


def date_words(iso_date: Any) -> str:
    """2026-07-31 -> 31 Jul 2026."""
    try:
        d = dt.date.fromisoformat(str(iso_date)[:10])
    except ValueError:
        return str(iso_date)
    return f"{d.day} {month(d, True)} {d.year}"


def cut(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- personal data in arguments

LONG_DIGITS = re.compile(r"\d[\d -]{6,}\d")
DATES = re.compile(r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)")


def mask(s: Any) -> str:
    """Text an assistant passed, with long digit runs (card, bank and ID numbers) kept to their last four, the way qbd
    masks them in its answers."""
    def last_four(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m[0])
        return m[0] if len(digits) < 8 else f"***{digits[-4:]}"
    parts = DATES.split(str(s))  # dates such as 2026-07-23 are no ID number
    return "".join(p if i % 2 else LONG_DIGITS.sub(last_four, p) for i, p in enumerate(parts))


# ---------------------------------------------------------------- QuickBooks requests and reports

REQUESTS = {
    "HostQueryRq": "QuickBooks version check", "CompanyQueryRq": "Company details", "PreferencesQueryRq": "Company preferences",
    "TransactionQueryRq": "Transactions", "AccountQueryRq": "Chart of accounts", "CustomerQueryRq": "Customer list",
    "VendorQueryRq": "Vendor list", "EmployeeQueryRq": "Employee list", "OtherNameQueryRq": "Other names list",
    "ItemQueryRq": "Item list", "ItemInventoryQueryRq": "Inventory part list", "ItemInventoryAssemblyQueryRq": "Assembly list",
    "ItemNonInventoryQueryRq": "Non-inventory part list", "ItemServiceQueryRq": "Service item list",
    "ItemOtherChargeQueryRq": "Other charge list", "ItemGroupQueryRq": "Item group list", "ItemDiscountQueryRq": "Discount list",
    "ItemSitesQueryRq": "Stock by site", "InvoiceQueryRq": "Invoices", "BillQueryRq": "Bills", "CheckQueryRq": "Checks",
    "DepositQueryRq": "Deposits", "EstimateQueryRq": "Estimates", "SalesOrderQueryRq": "Sales orders",
    "SalesReceiptQueryRq": "Sales receipts", "CreditMemoQueryRq": "Credit memos", "ReceivePaymentQueryRq": "Customer payments",
    "ReceivePaymentToDepositQueryRq": "Payments waiting to be deposited", "ChargeQueryRq": "Statement charges",
    "VendorCreditQueryRq": "Vendor credits", "BillPaymentCheckQueryRq": "Bill payments by check",
    "BillPaymentCreditCardQueryRq": "Bill payments by card", "CreditCardChargeQueryRq": "Credit card charges",
    "CreditCardCreditQueryRq": "Credit card credits", "PurchaseOrderQueryRq": "Purchase orders", "ItemReceiptQueryRq": "Item receipts",
    "JournalEntryQueryRq": "Journal entries", "InventoryAdjustmentQueryRq": "Inventory adjustments",
    "BuildAssemblyQueryRq": "Assembly builds", "TransferQueryRq": "Transfers", "TransferInventoryQueryRq": "Inventory transfers",
    "SalesTaxPaymentCheckQueryRq": "Sales tax payments", "TimeTrackingQueryRq": "Time entries", "TxnDeletedQueryRq": "Deleted transactions",
    "ListDeletedQueryRq": "Deleted list records", "CompanyActivityQueryRq": "Company activity", "DataExtDefQueryRq": "Custom field list",
    "TemplateQueryRq": "Template list", "ToDoQueryRq": "To-do list", "TermsQueryRq": "Terms list", "ClassQueryRq": "Class list",
    "SalesTaxCodeQueryRq": "Sales tax code list", "PaymentMethodQueryRq": "Payment method list", "ShipMethodQueryRq": "Shipping method list",
    "PriceLevelQueryRq": "Price level list", "SalesRepQueryRq": "Sales rep list", "CurrencyQueryRq": "Currency list",
    "InventorySiteQueryRq": "Inventory site list", "CustomerTypeQueryRq": "Customer type list", "VendorTypeQueryRq": "Vendor type list",
    "JobTypeQueryRq": "Job type list", "CustomerMsgQueryRq": "Customer message list", "BillingRateQueryRq": "Billing rate list",
    "VehicleQueryRq": "Vehicle list", "PayrollItemWageQueryRq": "Payroll item list", "PayrollItemNonWageQueryRq": "Payroll item list",
    "TxnDelRq": "Delete a transaction", "ListDelRq": "Delete a list record", "TxnVoidRq": "Void a transaction",
}
REPORTS = {
    "ARAgingDetail": "A/R aging report", "ARAgingSummary": "A/R aging summary", "APAgingDetail": "A/P aging report",
    "APAgingSummary": "A/P aging summary", "BalanceSheetStandard": "Balance sheet", "ProfitAndLossStandard": "Profit and loss report",
    "ProfitAndLossDetail": "Profit and loss detail", "SalesByItemSummary": "Sales by item report",
    "SalesByCustomerSummary": "Sales by customer report", "SalesByRepSummary": "Sales by rep report",
    "SalesTaxLiability": "Sales tax liability report", "InventoryValuationDetail": "Inventory valuation detail",
    "InventoryValuationSummary": "Inventory valuation report", "TxnDetailByAccount": "Transactions by account report",
    "UnpaidBillsDetail": "Unpaid bills report", "CustomerBalanceSummary": "Customer balance report",
    "VendorBalanceSummary": "Vendor balance report", "ExpenseByVendorSummary": "Expenses by vendor report",
    "PurchaseByItemSummary": "Purchases by item report", "PurchaseByVendorSummary": "Purchases by vendor report",
    "JobProfitabilitySummary": "Job profitability report", "TimeByJobSummary": "Time by job report", "AuditTrail": "Audit trail report",
}
REPORT_QUERY = re.compile(r"(\w*Report)QueryRq")
WRITE = re.compile(r"(\w+?)(Add|Mod)Rq")


def _split(camel: str) -> str:
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", camel)
    return " ".join([words[0]] + [w if w.isupper() and len(w) > 1 else w.lower() for w in words[1:]]) if words else camel


def report_label(report: Any) -> str:
    if not isinstance(report, str) or not report:
        return "Report"
    return REPORTS.get(report) or _split(re.sub(r"(Standard|Summary|Detail)$", "", report)) + " report"


def request_label(request: Any, report: Any = None) -> str:
    """A QuickBooks request in plain words: CustomerQueryRq is "Customer list"."""
    name = str(request or "").split(",")[0]
    if report or REPORT_QUERY.fullmatch(name):
        return report_label(report) if report else _split(REPORT_QUERY.fullmatch(name)[1]) + " report"
    if name in REQUESTS:
        return REQUESTS[name]
    m = WRITE.fullmatch(name)
    if m:
        return f"Save {'a new' if m[2] == 'Add' else 'changes to a'} {_split(m[1]).lower()}"
    base = re.sub(r"(Query|Del|Void)?Rq$", "", name)
    return _split(base) + (" list" if name.endswith("QueryRq") else "") if base else "QuickBooks request"


def doing(request: Any, report: Any, paging: bool) -> str:
    """What QuickBooks is doing while it works on a request: building the A/R aging report…"""
    label = request_label(request, report)
    if report or REPORT_QUERY.fullmatch(str(request or "")):
        return f"QuickBooks is building the {label[0].lower() + label[1:] if not label[:3].isupper() else label}…"
    if WRITE.fullmatch(str(request or "")) or str(request or "") in ("TxnDelRq", "ListDelRq", "TxnVoidRq"):
        return "QuickBooks is saving the change…"
    what = label if label.isupper() else label[0].lower() + label[1:]
    return f"QuickBooks is finding the {what}…" if paging else f"QuickBooks is reading the {what}…"


def fold_steps(requests: Any) -> list[dict]:
    """The QuickBooks requests of one command as steps: consecutive pages of one query fold into one step.
    Each step: label, request, pages, records (or rows for a report), ms, status, report."""
    steps: list[dict] = []
    for r in requests if isinstance(requests, list) else []:
        if not isinstance(r, dict):
            continue
        name, report = r.get("request"), r.get("report")
        items = r.get("items") if isinstance(r.get("items"), int) else 0
        last = steps[-1] if steps else None
        if last is not None and last["request"] == name and not report and not last["report"]:
            last["pages"] += 1
            last["records"] += items
            last["ms"] += r.get("ms", 0) if isinstance(r.get("ms"), (int, float)) else 0
            last["status"] = r.get("status", last["status"])
            continue
        steps.append({"request": name, "label": request_label(name, report), "report": report, "pages": 1, "records": items,
                      "ms": r.get("ms", 0) if isinstance(r.get("ms"), (int, float)) else 0, "status": r.get("status"),
                      "counted": "items" in r})
    return steps


def step_text(s: dict) -> str:
    """5 pages · 1,000 records · 4.2 s; 58 rows · 0.54 s; 0.09 s."""
    bits = [plural(s["pages"], "page")] if s["pages"] > 1 else []
    if s.get("counted"):
        bits.append(plural(s["records"], "row") if s.get("report") else plural(s["records"], "record"))
    bits.append(secs(s["ms"], fine=True))
    return " · ".join(bits)


# ---------------------------------------------------------------- what went wrong, and what to do

CLOSED = {"0x80040401", "0x80040408", "0x80040416", "0x80040424"}
BLOCKED = {"0x80040414", "no-session"}
PERMISSION = {"0x80040418", "0x8004041A", "0x8004041D", "0x80040420"}

# refused drafts and commits: what refused it, by the words qbd uses
REFUSALS = [
    (re.compile(r"on or before the closing date"), "closing date"),
    (re.compile(r"above the limit of"), "amount limit"),
    (re.compile(r"records is not allowed here|cannot (add|change|be deleted|be voided)|records cannot be"), "record type"),
    (re.compile(r"already (has|exists|saved)|never confirmed it|was already undone"), "duplicate"),
    (re.compile(r"Changes are turned off"), "changes off"),
    (re.compile(r"Deleting and voiding are turned off"), "deletes off"),
    (re.compile(r"approved in the Activity Monitor \(QBD_REQUIRE"), "approve in the monitor"),
    (re.compile(r"rejected this change in the Activity Monitor"), "rejected"),
    (re.compile(r"draft expired|batch expired|Unknown or already used token|draft is gone"), "expired"),
]


def refusal(message: Any) -> str:
    """Which check refused a draft or commit: closing date, amount limit, record type, duplicate…; "" when none fits."""
    for pattern, kind in REFUSALS:
        if pattern.search(str(message or "")):
            return kind
    return ""


def first_sentence(s: Any) -> str:
    s = str(s or "").strip()
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", s, maxsplit=1)[0] if s else ""


def rest_of(s: Any) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", str(s or "").strip(), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def outcome_badge(outcome: Any, error: Any = "") -> tuple[str, str, str]:
    """A failed request's badge: (word, tone, icon). Tones: ok, warn, bad, info, neutral."""
    code = str(outcome or "")
    if code == "ok":
        return "Done", "ok", "check_circle"
    if code in BLOCKED:
        return "Blocked by a window", "bad", "window"
    if code in CLOSED:
        return "QuickBooks closed", "bad", "x"
    if code == "timeout":
        return "Timed out", "bad", "clock"
    if code == "busy":
        return "Busy", "warn", "clock"
    if code == "stopped":
        return "Stopped", "neutral", "stop_square"
    if code == "paused":
        return "Paused", "neutral", "pause"
    if code == "unconfirmed":
        return "Not confirmed", "bad", "clock"
    if code in ("invalid_arguments", "invalid_request"):
        return "Wrong request", "warn", "info"
    if code == "refused":
        kind = refusal(error)
        if kind == "rejected":
            return "Rejected", "neutral", "x"
        if kind == "approve in the monitor":
            return "Waiting for you", "warn", "person"
        return (f"Refused: {kind}" if kind else "Refused"), "bad", "slash"
    if code in ("qb_status_3260", "qb_status_3220") or code in PERMISSION:
        return "Not allowed", "bad", "x"
    if code.startswith("qb_status_"):
        return "QuickBooks refused", "bad", "x"
    return "Failed", "bad", "x"


def call_badge(e: dict) -> tuple[str, str, str]:
    """A finished request's badge; a draft that worked is a preview, not a change."""
    if e.get("outcome") == "ok" and e.get("command") in ("draft", "import", "undo"):
        return "Previewed, not saved", "warn", "approval"
    return outcome_badge(e.get("outcome"), e.get("error"))


def problem(e: dict, assistant: str, company: str) -> dict:
    """A failed request in plain words: {"text": what happened, "steps": what to do, numbered}."""
    code, error, hint = str(e.get("outcome") or ""), str(e.get("error") or ""), str(e.get("hint") or "")
    who = assistant if assistant != "Unknown assistant" else "the assistant"
    company = company or "the company file"
    if code in BLOCKED:
        steps = ["Switch to QuickBooks and close the open window or message. It may be hidden behind other windows.",
                 f"Ask {who} to try again."]
        if code == "no-session":
            steps.insert(1, "If it asks for \"Application permission\", choose \"Yes, whenever my QuickBooks company file is open\" "
                            "and click Continue.")
        return {"text": "A QuickBooks window was open and waiting for someone, so QuickBooks could not answer.", "steps": steps}
    if code in CLOSED:
        what = ("QuickBooks was still starting up" if code == "0x80040424" else "No company file was open in QuickBooks"
                if code == "0x80040416" else "QuickBooks wasn't open and couldn't be started")
        return {"text": f"{what}, so this request did not run.",
                "steps": [f"Open QuickBooks with {company} and leave it open while assistants work, or move their work to office hours.",
                          f"Ask {who} to run it again."]}
    if code == "timeout":
        return {"text": f"QuickBooks took longer than {span_of(error)} to answer, so {who} gave up. The request kept running "
                        "inside QuickBooks, and QuickBooks was slow until it finished.",
                "steps": ["Wait until QuickBooks is quick again before retrying; a second try only adds to the queue inside QuickBooks.",
                          "If it keeps happening at the same time of day, look for what else keeps QuickBooks busy then, such as a "
                          "backup or a large report."]}
    if code == "busy":
        return {"text": "Another request was using QuickBooks, and this one gave up after waiting its 60 s turn. Nothing was "
                        "read or changed.",
                "steps": ["Nothing to fix: it is safe to try again.",
                          "If it happens often, ask the assistants to read less at once or to work at different times."]}
    if code == "stopped":
        return {"text": error or "Stopped in the Activity Monitor. Nothing was saved.",
                "steps": [f"{who[0].upper() + who[1:]}'s next command waits until someone clicks Resume on the Live page."]}
    if code == "paused":
        return {"text": f"{who[0].upper() + who[1:]}'s session was paused in the Activity Monitor, so qbd did not run this request.",
                "steps": ["Click Resume on the Live page when the assistant may continue, then ask it to run the request again."]}
    if code == "unconfirmed":
        return {"text": "QuickBooks did not confirm this change in time, so it may or may not be saved.",
                "steps": ["Before anyone tries again, look for the record in QuickBooks, or ask the assistant to search for it.",
                          "Trying again without checking could save it twice."]}
    if code in ("invalid_arguments", "invalid_request"):
        return {"text": f"qbd did not understand what {who} asked for, so nothing was read or changed: {cut(error, 160)}",
                "steps": ["Nothing to fix in QuickBooks: assistants usually correct the request and try again."]}
    if code == "refused":
        kind = refusal(error)
        said = first_sentence(error)
        steps = {
            "closing date": [f"If the date was a mistake, ask {who} to use a date after the closing date.",
                             "If the change really belongs in the closed period, the owner makes it in QuickBooks with the "
                             "closing-date password."],
            "amount limit": [f"If the amount is right, a person enters it in QuickBooks, or the owner raises QBD_MAX_AMOUNT "
                             "(see Safety).", f"If it is not, ask {who} to check the amounts."],
            "record type": ["The owner decides which records assistants may change (QBD_WRITE_ENTITIES, see Safety).",
                            "Otherwise a person makes this change in QuickBooks."],
            "duplicate": ["Look for the record in QuickBooks first: it may be there already.",
                          f"If it is really a second one, ask {who} to say so (allow_duplicate)."],
            "changes off": ["This PC is read-only. The owner can allow changes (QBD_ALLOW_WRITES, see Safety)."],
            "deletes off": ["Deleting and voiding are off on this PC. A person does it in QuickBooks, or the owner turns on "
                            "QBD_ALLOW_DELETE (see Safety)."],
            "approve in the monitor": ["Answer the draft under Approvals; the assistant's commit then gets the result."],
            "rejected": ["Nothing was saved. Tell the assistant what to change if the change is still needed."],
            "expired": [f"Nothing was saved. Ask {who} to prepare the change again if it is still needed."],
        }.get(kind) or ([rest_of(error)] if rest_of(error) else [f"Nothing was saved. Ask {who} what it needs."])
        return {"text": f"qbd refused it. {dates(said)} Nothing was saved." if said else "qbd refused it. Nothing was saved.",
                "steps": [dates(x) for x in steps]}
    if code.startswith("qb_status_"):
        status = code[10:]
        tip = STATUS_HINTS.get(int(status)) if status.isdigit() else None
        return {"text": f"QuickBooks refused it. {first_sentence(error) or 'The technical details have its answer.'}",
                "steps": [f"{tip or hint or 'Ask the assistant to check the request.'} QuickBooks status {status}."]}
    if code in HINTS or code.startswith("0x"):
        return {"text": f"qbd could not reach QuickBooks: {first_sentence(error)}",
                "steps": [hint or HINTS.get(code) or "Run qbd doctor on this PC to see what is missing."]}
    return {"text": f"qbd stopped with an unexpected error: {cut(first_sentence(error), 160)}",
            "steps": ["Try again. If it happens again, send the technical details to whoever set up qbd."]}


def dates(s: str) -> str:
    """Dates in qbd's messages (2026-07-31) as people write them (31 Jul 2026)."""
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", lambda m: date_words(m[0]), s)


def span_of(error: str) -> str:
    m = re.search(r"within ([\d.]+) s", error)
    return span(float(m[1])) if m else "the time allowed"


def change_badge(outcome: Any) -> tuple[str, str, str]:
    """A change's result as the write log has it."""
    return {"done": ("Saved", "ok", "check_circle"), "failed": ("Not saved", "bad", "x"), "unknown": ("Check it", "warn", "info"),
            "rejected": ("Rejected", "neutral", "x")}.get(str(outcome), ("Check it", "warn", "info"))


# ---------------------------------------------------------------- arguments, for rows and for "What it asked for"

HIDDEN = {"fields", "body", "reason", "token"}


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return num(v)
    if isinstance(v, list):
        return ", ".join(_value(x) for x in v)
    s = mask(v)
    return s.replace("_", " ") if re.fullmatch(r"[a-z0-9_]+", s) else s


def args_line(e: dict) -> str:
    """A request's arguments in a few words, for its row: invoices · "Coastal Freight Lines" · days 30."""
    args = e.get("args")
    if isinstance(args, list):  # the command line as typed, when it could not be read
        return cut(mask(" ".join(map(str, args))), 60) if args else ""
    if not isinstance(args, dict):
        return ""
    if not args:
        return "defaults"
    bits = []
    for k, v in args.items():
        if k in HIDDEN or v is None or isinstance(v, dict):
            continue
        if isinstance(v, bool):
            bits.append(k.replace("_", " ") if v else f"no {k.replace('_', ' ')}")
        elif isinstance(v, str) and not re.fullmatch(r"[a-z0-9_]+|[\d.,-]+", v):
            bits.append(f'"{mask(v)}"')
        elif isinstance(v, (int, float)) or re.fullmatch(r"[\d.,-]+", str(v)):
            bits.append(f"{k.replace('_', ' ')} {_value(v)}")
        else:
            bits.append(_value(v))
    return cut(" · ".join(bits), 80)


def _label(name: str, help_text: str) -> str:
    h = re.sub(r"\s*\(.*?\)\s*", " ", help_text or "").strip().rstrip(".")
    return cut(h if 0 < len(h) <= 40 else name.replace("_", " ").capitalize(), 40)


def asked_for(command: Any, args: Any, tools: dict | None) -> list[tuple[str, str]]:
    """What a request asked for, one (label, value) per argument, with the defaults that applied marked (default).
    tools: qbd's command table, for the arguments' names in words and their defaults."""
    t = (tools or {}).get(command)
    given = args if isinstance(args, dict) else {}
    rows: list[tuple[str, str]] = []
    spec = getattr(t, "input", {}) or {}
    for k, v in given.items():
        if k in ("token",):
            continue
        arg = spec.get(k)
        value = f"{plural(len(v), 'field')}: {', '.join(map(str, v))}" if k == "fields" and isinstance(v, list) else _value(v)
        rows.append((_label(k, getattr(arg, "help", "")), cut(value, 120)))
    for k, arg in spec.items():
        if k in given or k in HIDDEN:
            continue
        default = getattr(arg, "default", None)
        if default is None or default is MISSING or default in ("", [], {}) or default is False:
            continue
        rows.append((_label(k, getattr(arg, "help", "")), f"{_value(default)} (default)"))
    return rows


# ---------------------------------------------------------------- who asked

def initials(name: str) -> str:
    """C for Claude, G for Gemini, n8 for n8n, ? for an unknown assistant."""
    word = (name or "?").split()[0] if (name or "").split() else "?"
    if word == "Unknown" or word == "?":
        return "?"
    return word[:2] if re.search(r"\d", word[:3]) else word[0].upper()


def program_name(program: Any) -> str:
    """agy.exe -> agy."""
    return re.sub(r"\.exe$", "", str(program or ""), flags=re.I)


def windows_user(user: Any) -> str:
    """frontdesk@BAYSIDE-PC -> frontdesk."""
    return str(user or "").split("@")[0]
