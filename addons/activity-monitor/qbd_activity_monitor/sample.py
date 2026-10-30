"""The design's sample morning at Bayside Hardware & Supply, written as qbd would have written it: three assistants,
a paged read running, two commands in line, two drafts waiting, and the problems and changes of the last days. For
the monitor's tests and screenshots, and for `python -m qbd_agent_kit.addons activity-monitor --sample`. Customer and
vendor names come from the kit's sample company; nothing here is real data."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Any

from qbd_agent_kit.api import dumps


BAYSIDE = "C:\\Users\\Public\\Documents\\Intuit\\QuickBooks\\Company Files\\Bayside.qbw"
ARCHIVE = "C:\\Users\\Public\\Documents\\Intuit\\QuickBooks\\Company Files\\Bayside 2025 archive.qbw"
COMPANY = "Bayside Hardware & Supply"
MARK = "monitor-sample.txt"  # its live files name no real process, so the monitor takes them as they are

CLAUDE = {"agent": "Claude (Claude Code)", "user": "frontdesk@BAYSIDE-PC"}
GEMINI = {"agent": "Gemini (agy)", "user": "frontdesk@BAYSIDE-PC"}
N8N = {"agent": "n8n nightly sync", "user": "svc-n8n@BAYSIDE-PC"}
NOBODY = {"user": "svc-backup@BAYSIDE-PC"}
ENTITIES = ["customer", "vendor", "invoice", "bill", "deposit", "receive_payment"]
READ = {"writes": False}
WRITE = {"writes": True, "deletes": False, "entities": ENTITIES, "max_amount": 10000}
DELETES = {**WRITE, "deletes": True}
# The PC's settings in the sample: changes allowed for these record types, no deletes.
ENV = {"QBD_ALLOW_WRITES": "true", "QBD_ALLOW_DELETE": "false", "QBD_WRITE_ENTITIES": ",".join(ENTITIES), "QBD_MAX_AMOUNT": "10000"}

TITLES = {"status": "QuickBooks connection status", "count": "Count QuickBooks records", "profile": "Company file profile", "receivables": "Who owes money (collections list)",
          "payables": "Bills to pay", "cash-position": "Cash position and short-term forecast", "deposits": "Payments waiting to be deposited",
          "search": "Search QuickBooks records", "draft": "Draft a change to QuickBooks", "commit": "Save an approved draft to QuickBooks",
          "export": "Export records to a CSV file", "find-duplicates": "Find duplicate entries", "report": "Run a QuickBooks report",
          "health-check": "Books health check (month-end readiness)", "purchasing": "Purchase orders to chase"}

HOST = {"request": "HostQueryRq", "ms": 90, "bytes": 1650}


def q(request: str, items: int, ms: int, **more: Any) -> dict:
    return {"request": request, "ms": ms, "bytes": 400 + items * 60, "elements": ["IncludeRetElement"], "status": 0, "items": items, **more}


STEPS = {
    "status": [HOST, q("CompanyQueryRq", 1, 120), q("PreferencesQueryRq", 1, 140)],
    "profile": [HOST, q("CompanyQueryRq", 1, 110), q("PreferencesQueryRq", 1, 130), q("AccountQueryRq", 16, 420)],
    "receivables": [HOST, q("AgingReportQueryRq", 58, 540, report="ARAgingDetail"), q("InvoiceQueryRq", 3, 130), q("CustomerQueryRq", 3, 110)],
    "payables": [HOST, q("BillQueryRq", 27, 420)],
    "cash-position": [HOST, q("AccountQueryRq", 8, 240), q("ReceivePaymentToDepositQueryRq", 4, 260)],
    "deposits": [HOST, q("ReceivePaymentToDepositQueryRq", 9, 380)],
}


class Writer:
    def __init__(self, folder: str, now: float):
        self.folder, self.now = folder, now
        self.midnight = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        self.calls: list[dict] = []
        self.audit: list[dict] = []

    def at(self, hms: str, day: int = 0) -> float:
        h, m, *s = (int(x) for x in hms.split(":"))
        return (self.midnight + dt.timedelta(days=day, hours=h, minutes=m, seconds=s[0] if s else 0)).timestamp()

    @staticmethod
    def iso(t: float) -> str:
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    def call(self, hms: str, who: dict, session: str, command: str, args: dict | None = None, *, day: int = 0, ms: int = 0,
             steps: list | None = None, outcome: str = "ok", error: str = "", hint: str = "", settings: dict | None = None,
             title: str = "", file: str = BAYSIDE, known_file: bool = True) -> None:
        steps = STEPS.get(command, [HOST]) if steps is None else steps
        if outcome != "ok" and not known_file:
            steps = []
        e = {"time": self.iso(self.at(hms, day)), "user": who["user"], **{k: v for k, v in who.items() if k != "user"},
             **({"session": session} if session and "agent" in who else {}), **({"session_title": title} if title else {})}
        if known_file:
            e.update(company=COMPANY, company_file=file)
        e.update(settings=settings or READ, command=command, title=TITLES.get(command, command), args=args or {}, outcome=outcome)
        if outcome != "ok":
            e["error"] = error
            if hint:
                e["hint"] = hint
        e.update(ms=ms or sum(s.get("ms", 0) for s in steps) + 60, items=sum(s.get("items", 0) for s in steps),
                 output_bytes=900, requests=steps)
        self.calls.append(e)

    def change(self, hms: str, who: dict, session: str, entry: dict, *, day: int = 0) -> None:
        self.audit.append({"time": self.iso(self.at(hms, day)), "user": who["user"], "companyFile": BAYSIDE,
                           **{k: v for k, v in who.items() if k != "user"}, **({"session": session} if session else {}), **entry})

    def write(self) -> None:
        os.makedirs(self.folder, exist_ok=True)
        for name, rows in (("calls.jsonl", self.calls), ("audit.jsonl", self.audit)):
            rows.sort(key=lambda e: e["time"])
            with open(os.path.join(self.folder, name), "w", encoding="utf-8", newline="\n") as f:
                f.writelines(dumps(e) + "\n" for e in rows)


def write_sample(folder: str, now: float | None = None) -> str:
    """Writes the sample into folder (made empty first if it holds an earlier sample) and returns it."""
    now = time.time() if now is None else now
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(os.path.join(folder, MARK)):
        import shutil

        shutil.rmtree(folder)
    elif os.listdir(folder):
        raise FileExistsError(f"{folder} is not empty and holds no sample: choose an empty folder")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, MARK), "w", encoding="utf-8") as f:
        f.write("The Activity Monitor's sample data (Bayside Hardware & Supply). Safe to delete.\n")
    s = Writer(folder, now)

    # -- Monday 19 October (two days ago)
    s.call("02:00:05", N8N, "n8n-1019", "export", {"type": "invoices"}, day=-2, steps=[HOST, q("InvoiceQueryRq", 398, 1800)],
           title="Nightly export of yesterday's invoices and payments")
    s.call("10:15:02", GEMINI, "agy-1015", "receivables", day=-2, ms=120000, steps=[HOST, {"request": "AgingReportQueryRq", "ms": 119800, "bytes": 0}],
           outcome="timeout", error="QuickBooks did not answer within 120 s. The request may still be running inside QuickBooks.",
           hint="Wait until QuickBooks is responsive again, then retry with a smaller date range or limit.", title="Collections list for the weekly call")
    s.call("10:19:30", GEMINI, "agy-1015", "receivables", day=-2, ms=120000, steps=[HOST, {"request": "AgingReportQueryRq", "ms": 119800, "bytes": 0}],
           outcome="timeout", error="QuickBooks did not answer within 120 s. The request may still be running inside QuickBooks.",
           title="Collections list for the weekly call")
    s.call("10:31:10", GEMINI, "agy-1015", "receivables", day=-2, title="Collections list for the weekly call")
    s.call("14:00:30", CLAUDE, "cc-1400", "deposits", day=-2, settings=WRITE, title="Deposit Friday's cheques")
    s.call("14:03:10", CLAUDE, "cc-1400", "draft", {"action": "add", "type": "deposit", "fields": ["DepositLineAdd", "DepositToAccountRef", "TxnDate"],
           "reason": "Deposit Friday's cheques"}, day=-2, settings=WRITE, steps=[HOST, q("CompanyQueryRq", 1, 100), q("ReceivePaymentQueryRq", 3, 200)],
           title="Deposit Friday's cheques")
    s.call("14:05:40", CLAUDE, "cc-1400", "commit", {"token": "c0ffee01"}, day=-2, settings=WRITE, ms=120400,
           steps=[HOST, {"request": "DepositAddRq", "ms": 120000, "bytes": 0}], outcome="unconfirmed",
           error="QuickBooks did not confirm the change, so it may or may not be saved. Search for the record before trying again. (timeout)",
           title="Deposit Friday's cheques")
    s.change("14:05:40", CLAUDE, "cc-1400", {"token": "c0ffee01", "action": "add", "entity": "deposit", "total": 1980,
             "summary": "Add deposit to Chequing dated 2026-10-19, total 1,980.00\n- Granite Peak Builders: 980.00\n- Alder Street Cafe: 600.00\n- Foxhall Landscaping: 400.00",
             "reason": "Deposit Friday's cheques", "outcome": "unknown", "error": "QuickBooks did not answer within 120 s."}, day=-2)
    s.call("15:12:00", CLAUDE, "cc-1510", "search", {"type": "bills", "party": "Metro Electric Wholesale"}, day=-2, settings=DELETES,
           steps=[HOST, q("BillQueryRq", 6, 300)], title="Enter the Metro Electric Wholesale bills and remove the duplicate one")
    s.call("15:20:15", CLAUDE, "cc-1510", "draft", {"action": "add", "type": "bill", "fields": ["ExpenseLineAdd", "RefNumber", "TxnDate", "VendorRef"],
           "reason": "Enter the Metro Electric bill"}, day=-2, settings=DELETES, steps=[HOST, q("VendorQueryRq", 1, 120)], outcome="refused",
           error="The total 12,400.00 is above the limit of 10,000.00 (QBD_MAX_AMOUNT).",
           title="Enter the Metro Electric Wholesale bills and remove the duplicate one")
    s.call("15:22:00", CLAUDE, "cc-1510", "search", {"type": "vendors", "name": "Metro Electric Wholesale"}, day=-2, settings=DELETES,
           steps=[HOST, q("VendorQueryRq", 1, 100)], title="Enter the Metro Electric Wholesale bills and remove the duplicate one")
    s.change("11:20:00", {"user": "frontdesk@BAYSIDE-PC", "agent": "Claude (Claude Code)"}, "cc-1112",
             {"token": "deadbe01", "action": "update", "entity": "vendor", "summary": "Change vendor Pioneer Paint Distributors\n- Terms: Net 30 -> Net 15",
              "reason": "Shorter terms after the supplier's letter", "outcome": "rejected", "answered_in": "monitor"}, day=-2)
    s.call("11:15:00", GEMINI, "agy-1115", "search", {"type": "vendors", "name": "Pioneer Paint"}, day=-2, steps=[HOST, q("VendorQueryRq", 18, 200)],
           title="Tidy vendor terms after the supplier letters")

    # -- yesterday
    s.call("02:00:04", N8N, "n8n-1020", "export", {"type": "invoices"}, day=-1, ms=2100, outcome="0x80040408", known_file=False,
           error="Could not start QuickBooks.",
           hint="QuickBooks could not be started. Open QuickBooks Desktop and the company file yourself, then try again.",
           title="Nightly export of yesterday's invoices and payments")
    s.call("08:30:10", GEMINI, "agy-0830", "status", day=-1, title="Morning check: who owes us money, which bills to pay this week, how much cash do we have")
    s.call("08:30:50", GEMINI, "agy-0830", "receivables", day=-1, title="Morning check: who owes us money, which bills to pay this week, how much cash do we have")
    s.call("08:31:30", GEMINI, "agy-0830", "payables", {"days_ahead": 7}, day=-1, title="Morning check: who owes us money, which bills to pay this week, how much cash do we have")
    s.call("08:32:05", GEMINI, "agy-0830", "cash-position", day=-1, title="Morning check: who owes us money, which bills to pay this week, how much cash do we have")
    s.call("11:00:20", CLAUDE, "cc-1100", "search", {"type": "invoices", "party": "Juniper Dental"}, day=-1, settings=WRITE,
           steps=[HOST, q("InvoiceQueryRq", 3, 200)], title="Fix the July invoice for Juniper Dental")
    s.call("11:02:40", CLAUDE, "cc-1100", "draft", {"action": "update", "type": "invoice", "fields": ["TxnDate"], "reason": "Fix the July invoice date"},
           day=-1, settings=WRITE, steps=[HOST, q("InvoiceQueryRq", 1, 150)], outcome="refused",
           error="Invoice 3988 is dated 2026-07-28, on or before the closing date 2026-07-31. Closed periods are not changed from here.",
           title="Fix the July invoice for Juniper Dental")
    s.call("11:25:00", CLAUDE, "cc-1125", "search", {"type": "vendors", "name": "Summit Lumber Supply"}, day=-1, settings=WRITE,
           steps=[HOST, q("VendorQueryRq", 1, 100)], title="Enter the Summit Lumber bill from the email")
    s.call("11:27:30", CLAUDE, "cc-1125", "draft", {"action": "add", "type": "bill", "fields": ["ExpenseLineAdd", "RefNumber", "TxnDate", "VendorRef"],
           "reason": "Enter the Summit Lumber bill"}, day=-1, settings=WRITE, steps=[HOST, q("VendorQueryRq", 1, 120), q("AccountQueryRq", 1, 100),
           q("BillQueryRq", 12, 300)], title="Enter the Summit Lumber bill from the email")
    s.call("11:30:05", CLAUDE, "cc-1125", "commit", {"token": "5ab1e002"}, day=-1, settings=WRITE,
           steps=[HOST, {"request": "BillAddRq", "ms": 300, "bytes": 500, "status": 3140}], outcome="qb_status_3140",
           error='There is an invalid reference to QuickBooks Account "Job Materials" in the Bill.',
           hint="A referenced customer, vendor, item or account does not exist. Search for the exact name first.",
           title="Enter the Summit Lumber bill from the email")
    s.change("11:30:05", CLAUDE, "cc-1125", {"token": "5ab1e002", "action": "add", "entity": "bill", "total": 3870,
             "summary": "Add bill for Summit Lumber Supply dated 2026-10-20, number SL-5521, total 3,870.00\n- Job Materials: 3,870.00",
             "reason": "Enter the bill that came by email", "outcome": "failed",
             "error": 'QuickBooks status 3140: There is an invalid reference to QuickBooks Account "Job Materials" in the Bill.'}, day=-1)
    s.call("13:00:10", NOBODY, "", "status", day=-1)
    s.call("13:00:40", NOBODY, "", "profile", day=-1)
    s.call("15:44:00", GEMINI, "agy-1544", "search", {"type": "customers", "name": "Lakeside Event Center"}, day=-1, settings=WRITE,
           steps=[HOST, q("CustomerQueryRq", 0, 120)], title="Set up Lakeside Event Center as a customer")
    s.call("15:46:10", GEMINI, "agy-1544", "draft", {"action": "add", "type": "customer", "fields": ["Name", "Phone"], "reason": "New customer"},
           day=-1, settings=WRITE, steps=[HOST, q("CustomerQueryRq", 0, 100)], title="Set up Lakeside Event Center as a customer")
    s.call("15:47:20", GEMINI, "agy-1544", "commit", {"token": "1a4e0003"}, day=-1, settings=WRITE,
           steps=[HOST, {"request": "CustomerAddRq", "ms": 200, "bytes": 900, "status": 0, "items": 1}], title="Set up Lakeside Event Center as a customer")
    s.change("15:47:20", GEMINI, "agy-1544", {"token": "1a4e0003", "action": "add", "entity": "customer",
             "summary": "Add customer Lakeside Event Center", "reason": "New customer booked a December event", "outcome": "done",
             "ids": {"ListID": "80000041-1792600000", "FullName": "Lakeside Event Center"}}, day=-1)
    s.call("16:05:00", CLAUDE, "cc-1605", "search", {"type": "customers", "name": "Foxhall Landscaping"}, day=-1, settings=WRITE,
           steps=[HOST, q("CustomerQueryRq", 1, 100)], title="Invoice Foxhall Landscaping for the October supplies")
    s.call("16:10:30", CLAUDE, "cc-1605", "draft", {"action": "add", "type": "invoice", "fields": ["CustomerRef", "InvoiceLineAdd", "TxnDate"],
           "reason": "Invoice the October supplies"}, day=-1, settings=WRITE, steps=[HOST, q("ItemQueryRq", 4, 200)],
           title="Invoice Foxhall Landscaping for the October supplies")
    s.call("16:12:00", CLAUDE, "cc-1605", "commit", {"token": "f0c4a004"}, day=-1, settings=WRITE,
           steps=[HOST, {"request": "InvoiceAddRq", "ms": 400, "bytes": 1800, "status": 0, "items": 1}],
           title="Invoice Foxhall Landscaping for the October supplies")
    s.change("16:12:00", CLAUDE, "cc-1605", {"token": "f0c4a004", "action": "add", "entity": "invoice", "total": 2450,
             "summary": "Add invoice for Foxhall Landscaping dated 2026-10-20, number 4031, total 2,450.00\n- Mulch, 40 bags: 1,450.00\n- Pavers, 2 pallets: 1,000.00",
             "reason": "Invoice the October landscaping supplies", "outcome": "done", "ids": {"TxnID": "2F1-1792700000", "RefNumber": "4031"}}, day=-1)
    s.call("16:38:00", CLAUDE, "cc-1638", "search", {"type": "customers", "name": "Ironwood Fitness"}, day=-1, settings=WRITE,
           steps=[HOST, q("CustomerQueryRq", 1, 100)], title="Invoice Ironwood Fitness for the gym flooring")
    s.call("16:40:10", CLAUDE, "cc-1638", "draft", {"action": "add", "type": "invoice", "fields": ["CustomerRef", "InvoiceLineAdd", "TxnDate"],
           "reason": "Invoice the gym flooring"}, day=-1, settings=WRITE, steps=[HOST, q("ItemQueryRq", 11, 200)],
           title="Invoice Ironwood Fitness for the gym flooring")
    s.call("17:05:00", CLAUDE, "cc-1705", "status", day=-1, file=ARCHIVE, title="Last year's sales for the budget")
    s.call("17:08:30", CLAUDE, "cc-1705", "report", {"report": "sales_by_customer", "period": "last_year"}, day=-1, file=ARCHIVE,
           steps=[HOST, q("GeneralSummaryReportQueryRq", 64, 2400, report="SalesByCustomerSummary")], title="Last year's sales for the budget")
    s.call("17:10:00", CLAUDE, "cc-1705", "profile", day=-1, file=ARCHIVE, title="Last year's sales for the budget")
    s.call("17:20:00", CLAUDE, "cc-1720", "status", day=-1, title="Month-end check of open purchase orders")
    s.call("17:42:00", CLAUDE, "cc-1720", "purchasing", day=-1, steps=[HOST, q("PurchaseOrderQueryRq", 214, 2200)],
           title="Month-end check of open purchase orders")

    # -- today
    s.call("02:00:06", N8N, "n8n-1021", "export", {"type": "invoices"}, steps=[HOST, q("InvoiceQueryRq", 230, 1400)],
           title="Nightly export of yesterday's invoices and payments")
    for i, (kind, n) in enumerate((("receive_payment", 120), ("credit_memo", 12), ("customers", 30), ("bills", 15), ("deposits", 5))):
        s.call(f"02:0{1 + i // 2}:{10 + 20 * (i % 2):02d}", N8N, "n8n-1021", "export", {"type": kind},
               steps=[HOST, q("TransactionQueryRq", n, 300 + n * 4)], title="Nightly export of yesterday's invoices and payments")
    morning = "Morning check: who owes us money, which bills to pay this week, how much cash do we have"
    s.call("08:31:12", GEMINI, "agy-0831", "status", ms=500, title=morning)
    s.call("08:31:49", GEMINI, "agy-0831", "receivables", ms=912, title=morning)
    s.call("08:32:30", GEMINI, "agy-0831", "payables", {"days_ahead": 7}, ms=600, title=morning)
    s.call("08:33:05", GEMINI, "agy-0831", "cash-position", ms=700, title=morning)
    elm = "Fix the contact on Elm Grove School District"
    s.call("08:47:10", CLAUDE, "cc-0847", "search", {"type": "customers", "name": "Elm Grove School District"}, settings=WRITE,
           steps=[HOST, q("CustomerQueryRq", 1, 120)], title=elm)
    s.call("08:49:30", CLAUDE, "cc-0847", "draft", {"action": "update", "type": "customer", "fields": ["Contact"], "name": "Elm Grove School District",
           "reason": "Fix the contact name"}, settings=WRITE, steps=[HOST, q("CustomerQueryRq", 1, 110), q("CompanyQueryRq", 1, 90), q("PreferencesQueryRq", 1, 100)],
           title=elm)
    s.call("08:51:02", CLAUDE, "cc-0847", "commit", {"token": "e1a90005"}, settings=WRITE,
           steps=[HOST, {"request": "CustomerModRq", "ms": 210, "bytes": 1200, "status": 0}], title=elm)
    s.change("08:51:02", CLAUDE, "cc-0847", {"token": "e1a90005", "action": "update", "entity": "customer",
             "summary": "Change customer Elm Grove School District\n- Contact: Accounts Payable Office -> Dana Reid, Accounts Payable",
             "reason": "Fix the contact name the school sent in its email", "outcome": "done",
             "before": {"ListID": "80000012-1792500000", "Name": "Elm Grove School District", "Contact": "Accounts Payable Office"},
             "request": "<CustomerModRq><CustomerMod><ListID>80000012-1792500000</ListID><EditSequence>1792500100</EditSequence>"
                        "<Contact>Dana Reid, Accounts Payable</Contact></CustomerMod></CustomerModRq>",
             "ids": {"ListID": "80000012-1792500000", "FullName": "Elm Grove School District"}})
    deposit = "Deposit this week's cheques and enter the Coastal Freight bill"
    s.call("09:41:30", CLAUDE, "cc-0941", "status", settings=WRITE, title=deposit)
    s.call("09:44:02", CLAUDE, "cc-0941", "profile", settings=WRITE, ms=30500, outcome="0x80040414", known_file=False,
           error="A modal dialog box is showing in the QuickBooks user interface. Your application cannot access QuickBooks until the user dismisses the dialog box.",
           hint="A window or message box is open in QuickBooks. Close it (look for a dialog behind other windows), then try again.", title=deposit)
    s.call("09:51:03", CLAUDE, "cc-0941", "profile", settings=WRITE, ms=1200, title=deposit)
    s.call("09:55:00", CLAUDE, "cc-0941", "deposits", settings=WRITE, title=deposit)
    s.call("09:57:40", CLAUDE, "cc-0941", "draft", {"action": "add", "type": "deposit", "fields": ["DepositLineAdd", "DepositToAccountRef", "TxnDate"],
           "reason": "Deposit the 4 cheques received this week"}, settings=WRITE, ms=1100,
           steps=[HOST, q("CompanyQueryRq", 1, 100), q("PreferencesQueryRq", 1, 100), q("ReceivePaymentQueryRq", 3, 500)], title=deposit)
    s.call("10:00:20", CLAUDE, "cc-0941", "search", {"type": "vendors", "name": "Coastal Freight Lines"}, settings=WRITE, ms=300,
           steps=[HOST, q("VendorQueryRq", 1, 120)], title=deposit)
    s.call("10:01:12", CLAUDE, "cc-0941", "draft", {"action": "add", "type": "bill", "fields": ["ExpenseLineAdd", "RefNumber", "TxnDate", "VendorRef"],
           "reason": "Enter the Coastal Freight bill"}, settings=WRITE, ms=900,
           steps=[HOST, q("CompanyQueryRq", 1, 90), q("PreferencesQueryRq", 1, 100), q("VendorQueryRq", 1, 100), q("AccountQueryRq", 1, 110)], title=deposit)
    dupes = "Look for duplicate entries before month end"
    s.call("10:02:05", CLAUDE, "cc-7f3c", "status", ms=400, title=dupes)
    s.call("10:03:10", CLAUDE, "cc-7f3c", "count", {"type": "transactions", "from": "2026-07-23"}, ms=300,
           steps=[HOST], title=dupes)
    s.write()

    # -- the drafts waiting, and one that expired yesterday
    drafts = os.path.join(folder, "drafts")
    os.makedirs(drafts, exist_ok=True)

    def draft(token: str, created: float, **d: Any) -> None:
        body = {"token": token, "request": "<sample/>", "message": "", "company_file": BAYSIDE, "created": created,
                "expires": created + 900, "allow_duplicate": False, **CLAUDE, "settings": WRITE, **d}
        body.pop("user", None)
        with open(os.path.join(drafts, f"{token}.json"), "w", encoding="utf-8") as f:
            f.write(dumps(body))

    draft("a1b2c3d4", s.at("09:57:40"), action="add", entity="deposit", message="DepositAddRq", session="cc-0941", session_title=deposit,
          summary="Add deposit to Chequing dated 2026-10-21, taking in 4 customer payments, total 3,231.95\n- Foxhall Landscaping: 1,200.00\n"
                  "- Alder Street Cafe: 486.20\n- Harper & Lowe Architects: 1,045.75\n- Kestrel Auto Repair: 500.00",
          reason="Deposit the 4 cheques received this week", total=3231.95, warnings=[],
          checks=["Dated after the closing date, 2026-07-31", "Under the limit for changes, 10,000.00",
                  "The 4 payments exist and wait to be deposited", "Not saved before from the same source"])
    draft("e5f6a7b8", s.at("10:01:12"), action="add", entity="bill", message="BillAddRq", session="cc-0941", session_title=deposit,
          summary="Add bill for Coastal Freight Lines dated 2026-10-21, number CF-88213, due 2026-11-05 (Net 15), total 1,240.00\n"
                  "- Freight and Delivery, Freight inbound: 1,100.00\n- Freight and Delivery, Fuel surcharge: 140.00",
          reason="Enter the Coastal Freight bill that came by email today", total=1240,
          warnings=["Possible duplicate: bill CF-88190 for Coastal Freight Lines, dated 2026-10-19, is also for 1,240.00. Same vendor and "
                    "amount within 3 days."],
          checks=["Dated after the closing date, 2026-07-31", "Under the limit for changes, 10,000.00", "The vendor and account names it uses exist",
                  "Number CF-88213 not used for Coastal Freight Lines within 30 days of its date"])
    with open(os.path.join(drafts, "1f0e0006.expired.json"), "w", encoding="utf-8") as f:
        created = s.at("16:40:10", -1)
        f.write(dumps({"token": "1f0e0006", "action": "add", "entity": "invoice", "summary": "Add invoice for Ironwood Fitness dated 2026-10-20, total 6,180.00",
                       "reason": "Invoice the gym flooring", "company_file": BAYSIDE, "created": created, "expires": created + 900,
                       "total": 6180, "agent": CLAUDE["agent"], "session": "cc-1638"}))

    # -- what runs now: Claude's paged read, with n8n and Gemini in line
    live = os.path.join(folder, "live")
    os.makedirs(live, exist_ok=True)

    def live_file(pid: int, info: dict) -> None:
        with open(os.path.join(live, f"{pid}.json"), "w", encoding="utf-8") as f:
            f.write(dumps({"pid": pid, **info}))

    started = s.at("10:03:54")
    live_file(4412, {"state": "running", "started": s.iso(started), "queued": s.iso(started), "queued_ns": int(started * 1e9),
                     "running_since": s.iso(started), "agent": CLAUDE["agent"], "session": "cc-7f3c", "session_title": dupes,
                     "user": CLAUDE["user"], "command": "find-duplicates", "title": TITLES["find-duplicates"], "args": {},
                     "company_file": BAYSIDE, "company": COMPANY, "done": [{"request": "HostQueryRq", "ms": 100}],
                     "step": {"request": "TransactionQueryRq", "n": 7, "since": s.iso(s.at("10:04:11")), "page": 6, "page_size": 200,
                              "read": 1200, "left": 1600, "cap": 20000}})
    for pid, who, session, title, command, args, hms in ((5120, N8N, "n8n-manual", "Invoice export, run by hand", "export", {"type": "invoices"}, "10:04:00"),
                                                         (5133, GEMINI, "agy-1004", "Customers to call about overdue invoices", "receivables", {}, "10:04:09")):
        t = s.at(hms)
        live_file(pid, {"state": "waiting", "started": s.iso(t), "queued": s.iso(t), "queued_ns": int(t * 1e9), "agent": who["agent"],
                        "session": session, "session_title": title, "user": who["user"], "command": command, "title": TITLES[command], "args": args})

    # -- the last facts qbd read
    with open(os.path.join(folder, "quickbooks.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"files": {
            BAYSIDE: {"product": "QuickBooks Enterprise Solutions", "version": "24.0", "country": "CA", "qbxml": "16.0", "company": COMPANY,
                      "closing_date": "2026-07-31", "by": CLAUDE["agent"], "read": s.iso(s.at("10:02:05"))},
            ARCHIVE: {"product": "QuickBooks Enterprise Solutions", "version": "24.0", "country": "CA", "qbxml": "16.0", "company": COMPANY,
                      "closing_date": "2025-12-31", "by": CLAUDE["agent"], "read": s.iso(s.at("17:10:00", -1))}},
            "last": BAYSIDE}))
    return folder


def design_seen(now: float) -> float:
    """Until when someone had looked at the problems in the design: only the blocked request of 09:44 is new."""
    return dt.datetime.fromtimestamp(now).replace(hour=9, minute=0, second=0, microsecond=0).timestamp()


def design_now() -> float:
    """The design's "now": Wednesday 21 October 2026, 10:04:12 on this PC's clock."""
    return dt.datetime(2026, 10, 21, 10, 4, 12).timestamp()
