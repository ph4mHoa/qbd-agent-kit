import os
import re
import tempfile
import time
import unittest
from unittest import mock

from qbd_agent_kit.config import load_config
from qbd_agent_kit.demo.transport import DemoTransport, Status
from qbd_agent_kit.errors import QbConnectionError, QbStatusError, QbxmlInputError, ToolError, WriteUnconfirmed
from qbd_agent_kit.jsutil import MISSING, dumps, js_round, num_str
from qbd_agent_kit.qb.client import HostInfo, QbClient
from qbd_agent_kit.qb.dates import macro_range
from qbd_agent_kit.safety.audit import committed
from qbd_agent_kit.safety.privacy import mask_personal_data
from qbd_agent_kit.tools import run_tool, tools_for
from qbd_agent_kit.tools.common import Ctx, Drafts
from qbd_agent_kit.qbxml.parse import Report, ReportColumn, ReportRow
from qbd_agent_kit.tools.insights import account_row, normalize_name, open_items, report_day
from qbd_agent_kit.tools.read import intuit_support, render_report
from qbd_agent_kit.tools.write import draft_total

TODAY = "2026-10-20"
AUDIT = os.path.join(tempfile.gettempdir(), f"qbd-agent-kit-test-{os.getpid()}.jsonl")


def demo(**env: str) -> tuple[Ctx, callable]:
    config = load_config({"QBD_AUDIT_LOG": AUDIT, **env}, demo=True)
    ctx = Ctx(qb=QbClient(DemoTransport(TODAY)), config=config, today=lambda: TODAY, drafts=Drafts(), cache={})
    tools = {t.name: t for t in tools_for(config)}

    def call(name: str, args: dict | None = None):
        return run_tool(ctx, tools[name], args or {})

    return ctx, call


class Setup(unittest.TestCase):
    def test_writes_are_off_unless_asked_for_outside_the_demo(self):
        names = [t.name for t in tools_for(load_config({}))]
        self.assertIn("qb_daily_brief", names)
        self.assertNotIn("qb_draft", names)
        self.assertNotIn("qb_commit", names)
        self.assertTrue(any(t.name == "qb_commit" for t in tools_for(load_config({"QBD_ALLOW_WRITES": "true"}))))


class Workflows(unittest.TestCase):
    def test_daily_brief_covers_cash_receivables_payables_sales_and_stock(self):
        _, call = demo()
        b = call("qb_daily_brief")
        self.assertEqual(b["as_of"], TODAY)
        self.assertGreater(b["cash"]["bank_total"], 0)
        self.assertGreater(b["receivables"]["overdue"], 0)
        self.assertTrue(b["receivables"]["top_overdue"])
        self.assertGreater(b["payables"]["overdue_bills"], 0)
        self.assertIsInstance(b["sales"]["month_to_date"]["income"], (int, float))
        self.assertEqual(b["inventory"]["negative_on_hand"], 1)
        self.assertGreaterEqual(len(b["attention"]), 3)
        self.assertNotIn("errors", b)

    def test_receivables_put_the_most_overdue_customer_first_and_flag_unapplied_payments(self):
        _, call = demo()
        r = call("qb_receivables", {"top": 20})
        overdue = [c["overdue"] for c in r["customers"]]
        self.assertEqual(overdue, sorted(overdue, reverse=True))
        self.assertTrue(r["customers"][0]["email"].endswith(".example"))
        # Its payment has no open invoice to go against, so there is nothing to chase.
        self.assertFalse(any(c["customer"] == "Juniper Dental Group" for c in r["customers"]))
        juniper = next(c for c in r["customers_in_credit"] if c["customer"] == "Juniper Dental Group")
        self.assertEqual(juniper["unapplied_payments_or_credits"], 500)
        self.assertIn("refund", juniper["next_step"])

    def test_open_receivables_and_payables_equal_the_agings_whatever_a_bills_open_amount_says(self):
        _, call = demo()
        ar = call("qb_report", {"report": "ar_aging"})["rows"][-1][-1]
        ap = call("qb_report", {"report": "ap_aging"})["rows"][-1][-1]
        # The demo, like QuickBooks, reports the vendor's whole open balance as each open bill's OpenAmount.
        bills = call("qb_raw", {"request": "BillQueryRq", "body": {
            "EntityFilter": {"FullName": "Coastal Freight Lines"}, "PaidStatus": "NotPaidOnly", "IncludeRetElement": ["OpenAmount"],
        }})
        self.assertEqual([b["OpenAmount"] for b in bills["rows"]], [877.4, 877.4])
        self.assertFalse(any("OpenAmount" in b for b in call("qb_search", {"type": "bill", "limit": 5})["rows"]))

        p = call("qb_payables")
        r = call("qb_receivables")
        b = call("qb_daily_brief")
        self.assertEqual(p["totals"]["open"], ap)
        self.assertEqual(b["payables"]["open_total"], ap)
        self.assertEqual(r["totals"]["open"], ar)
        self.assertEqual(b["receivables"]["total_owed"], ar)
        rec = b["receivables"]
        self.assertEqual(js_round((rec["current"] + rec["overdue"] - rec["credit_balances"]) * 100) / 100, ar)
        # A customer in credit lowers the A/R total but not what is overdue, so both lists show what it held back.
        self.assertEqual(r["totals"]["credit_balances"], rec["credit_balances"])
        owed = sum(c["open"] for c in call("qb_receivables", {"top": 100})["customers"])
        self.assertEqual(js_round((owed - r["totals"]["credit_balances"]) * 100) / 100, ar)

    def test_a_vendor_credit_goes_against_the_vendors_oldest_bill_and_is_flagged(self):
        _, call = demo()
        p = call("qb_payables", {"vendor": "Coastal Freight Lines"})
        self.assertEqual(p["vendors"][0]["open"], 877.4)
        self.assertEqual(p["vendors"][0]["unapplied_credits"], 95)
        self.assertEqual([[x["ref"], x["due"], x["amount"], x.get("open_on_bill")] for x in p["bills_to_pay"]], [
            ["CF-1182", "2026-10-23", 391.2, 486.2],
            ["CF-1182", "2026-10-25", 486.2, None],
        ])
        self.assertEqual(p["totals"]["cash_needed_now_and_soon"], 877.4)
        b = call("qb_daily_brief")
        self.assertEqual(b["payables"]["unapplied_credits"], 95)
        self.assertTrue(any(x.startswith("95.00 in vendor credits") for x in b["attention"]))

    def test_duplicate_finder_catches_the_planted_duplicates(self):
        _, call = demo()
        d = call("qb_find_duplicates")
        kinds = [f"{f['kind']}:{f['confidence']}" for f in d["findings"]]
        for k in ("bill entered twice:high", "bill also paid directly:high", "customer listed twice:high", "possible duplicate bill:medium"):
            self.assertIn(k, kinds)
        self.assertEqual(len([k for k in kinds if k.startswith("customer listed twice")]), 1)
        # QuickBooks sends bill amounts negative in a transaction query; findings show their size.
        twice = next(f for f in d["findings"] if f["kind"] == "bill entered twice")
        self.assertEqual([r["amount"] for r in twice["records"]], [486.2, 486.2])

    def test_health_check_reports_each_planted_problem(self):
        _, call = demo()
        h = call("qb_health_check")
        status = {c["check"]: c["status"] for c in h["checks"]}
        self.assertEqual(h["period_end"], "2026-09-30")
        for k in ("suspense_accounts", "undeposited_funds", "unapplied_payments", "old_receivables", "unapplied_vendor_credits", "negative_inventory", "future_dated"):
            self.assertEqual(status[k], "warn", k)
        self.assertEqual(status["closing_date"], "ok")
        self.assertEqual(status["stale_documents"], "info")
        stale = next(c for c in h["checks"] if c["check"] == "stale_documents")
        self.assertEqual([(d["ref"], d["amount"]) for d in stale["details"]], [("SO-880", 242.89), ("PO-1468", 246)])
        self.assertEqual(h["summary"]["errors"], 0)

    def test_stock_shows_each_site_and_puts_oversold_rows_first(self):
        ctx, call = demo()
        s = call("qb_stock", {"limit": 2000})
        self.assertIs(s["by_site"], True)
        for item in ctx.qb.transport.company.lists["ItemInventory"]:
            here = [r for r in s["rows"] if r["item"] == item["FullName"]]
            self.assertEqual(sorted(r["site"] for r in here), ["Main Store", "Warehouse"])
            self.assertEqual(sum(r["on_hand"] for r in here), item["QuantityOnHand"])
        self.assertTrue(all(r["available"] == r["on_hand"] - r["on_sales_orders"] for r in s["rows"]))

        short = call("qb_stock", {"short": True})["rows"]
        self.assertEqual(len(short), s["oversold"] + s["at_reorder_point"])
        oversold = [r["available"] for r in short if r["status"] == "oversold"]
        self.assertEqual(oversold, sorted(oversold))
        self.assertEqual([r["status"] for r in short], ["oversold"] * len(oversold) + ["reorder"] * (len(short) - len(oversold)))
        screws = next(r for r in short if r["item"] == "Deck Screws 5lb")
        warehouse = next(r for r in s["rows"] if r["item"] == "Deck Screws 5lb" and r["site"] == "Warehouse")
        self.assertEqual(screws["available_elsewhere"], warehouse["available"])

        one = call("qb_stock", {"items": ["Deck Screws 5lb", "No Such Item"], "site": "warehouse"})
        self.assertEqual([(r["item"], r["site"]) for r in one["rows"]], [("Deck Screws 5lb", "Warehouse")])
        self.assertEqual(one["not_found"], ["No Such Item"])
        with self.assertRaisesRegex(ToolError, 'No site named "Attic". Sites: Main Store, Warehouse'):
            call("qb_stock", {"site": "Attic"})

    def test_stock_is_company_wide_when_sites_are_off(self):
        ctx, call = demo()
        request = ctx.qb.request

        def no_sites(message, body, attrs=None):
            rs = request(message, body, attrs)
            if message == "PreferencesQueryRq":
                rs.body["PreferencesRet"].pop("MultiLocationInventoryPreferences", None)
            return rs

        ctx.qb.request = no_sites
        s = call("qb_stock", {"items": ["Deck Screws 5lb"]})
        self.assertIs(s["by_site"], False)
        item = next(i for i in ctx.qb.transport.company.lists["ItemInventory"] if i["FullName"] == "Deck Screws 5lb")
        self.assertEqual(s["rows"], [{"item": "Deck Screws 5lb", "on_hand": item["QuantityOnHand"], "on_sales_orders": item["QuantityOnSalesOrder"],
                                      "available": item["QuantityOnHand"] - item["QuantityOnSalesOrder"], "on_purchase_orders": item["QuantityOnOrder"],
                                      "reorder_point": item["ReorderPoint"], "status": "oversold"}])
        with self.assertRaisesRegex(ToolError, "sites are off"):
            call("qb_stock", {"site": "Warehouse"})

    def test_backorders_give_stock_to_the_oldest_orders_and_show_what_is_on_the_way(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        b = call("qb_backorders")
        screws = next(x for x in b["short"] if x["item"] == "Deck Screws 5lb")
        self.assertEqual((screws["so"], screws["can_ship"], screws["short_qty"], screws["not_on_order"]), ("SO-883", 14, 9, 9))
        self.assertEqual(screws["free_at_other_sites"], 8)  # the warehouse has some: a transfer would cover most of it
        self.assertEqual(b["summary"]["short_lines"], len(b["short"]))
        stock = {(r["item"], r["site"]): r["on_hand"] for r in call("qb_stock", {"limit": 2000})["rows"]}
        for item, site in {(x["item"], x["site"]) for x in b["ready"]}:
            self.assertLessEqual(sum(x["open_qty"] for x in b["ready"] if (x["item"], x["site"]) == (item, site)), stock[(item, site)])

        # An older order for the same item comes first, even when it is older than the orders read.
        item = next(i for i in c.lists["ItemInventory"] if i["FullName"] == "Deck Screws 5lb")
        old = next(t for t in c.txns["SalesOrder"] if t["RefNumber"] == "SO-880")
        old["SalesOrderLineRet"].append({"TxnLineID": "O0-X", "ItemRef": {"ListID": item["ListID"], "FullName": item["FullName"]}, "Quantity": 10, "Rate": 1, "Amount": 10})
        item["QuantityOnSalesOrder"] += 10
        for args in ({}, {"from": "2026-10-01"}):
            x = next(x for x in call("qb_backorders", args)["short"] if x["item"] == "Deck Screws 5lb" and x["so"] == "SO-883")
            self.assertEqual((x["can_ship"], x["short_qty"]), (4, 19))

        # A purchase order on the way covers the short lines, oldest order first.
        c.txns["PurchaseOrder"].append({"TxnID": "ZZ-PO", "TxnDate": TODAY, "RefNumber": "PO-1600", "VendorRef": {"FullName": "Harbor Hardware Wholesale"},
                                        "ExpectedDate": "2026-10-27", "IsManuallyClosed": False, "IsFullyReceived": False,
                                        "PurchaseOrderLineRet": [{"TxnLineID": "P-1", "ItemRef": {"FullName": "Deck Screws 5lb"}, "Quantity": 30, "ReceivedQuantity": 0}]})
        x = next(x for x in call("qb_backorders")["short"] if x["item"] == "Deck Screws 5lb" and x["so"] == "SO-883")
        self.assertEqual((x["eta"], x["on_order"][0]["po"]), ("2026-10-27", "PO-1600"))
        self.assertNotIn("not_on_order", x)
        self.assertEqual({x["customer"] for x in [*call("qb_backorders", {"customer": "Kestrel Auto Repair"})["ready"]]}, {"Kestrel Auto Repair"})

    def test_price_check_finds_sales_below_the_item_list_and_the_customers_price_level(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        c.lists["PriceLevel"] = [
            {"ListID": "PL-1", "Name": "Contractor", "IsActive": True, "PriceLevelType": "FixedPercentage", "PriceLevelFixedPercentage": -10},
            {"ListID": "PL-2", "Name": "Marina", "IsActive": True, "PriceLevelType": "PerItem",
             "PriceLevelPerItemRet": [{"ItemRef": {"FullName": "LED Flood Light 50W"}, "CustomPrice": 49.99}]},
        ]
        customer = {x["FullName"]: x for x in c.lists["Customer"]}
        customer["Alder Street Cafe"]["PriceLevelRef"] = {"ListID": "PL-1", "FullName": "Contractor"}
        customer["Cedar Point Marina"]["PriceLevelRef"] = {"ListID": "PL-2", "FullName": "Marina"}
        invoice = {t["RefNumber"]: t for t in c.txns["Invoice"]}

        def price(ref: str, item: str, rate: float) -> None:
            line = next(x for x in invoice[ref]["InvoiceLineRet"] if x["ItemRef"]["FullName"] == item)
            line.update(Rate=rate, Amount=round(rate * line["Quantity"], 2))

        price("1088", "Exterior Paint 1gal White", 30)  # Contractor price 34.20 (38 less 10%)
        price("1054", "Primer 1gal", 20)  # not on the Marina list: the standard 24
        price("1042", "Pressure-Treated Post 4x4x8", 19.9)  # under 1% below
        c.lists["ItemDiscount"] = [{"ListID": "D-1", "Name": "Loyalty Discount", "FullName": "Loyalty Discount", "IsActive": True}]
        invoice["1088"]["InvoiceLineRet"].append({"TxnLineID": "1088-X", "ItemRef": {"FullName": "Loyalty Discount"}, "RatePercent": -5, "Amount": -24})
        r = call("qb_price_check")
        self.assertEqual(r["period"], {"from": "2026-07-22", "to": TODAY})
        self.assertEqual(r["below_list"], {"lines": 2, "gap": 139.2})
        self.assertEqual([(x["ref"], x["item"], x["rate"], x["expected"], x.get("price_level"), x["gap"]) for x in r["lines"]],
                         [("1054", "Primer 1gal", 20, 24, "Marina", 72), ("1088", "Exterior Paint 1gal White", 30, 34.2, "Contractor", 67.2)])
        self.assertEqual(r["by_customer"], [{"customer": "Cedar Point Marina", "lines": 1, "gap": 72}, {"customer": "Alder Street Cafe", "lines": 1, "gap": 67.2}])
        self.assertEqual(r["discount_lines"], {"lines": 1, "total": -24})
        self.assertNotIn("1066", [x["ref"] for x in r["lines"]], "the Marina price for the flood light is 49.99, and it sold at 54.99")
        self.assertEqual(call("qb_price_check", {"min_gap_percent": 0})["below_list"]["lines"], 3)
        self.assertEqual([x["ref"] for x in call("qb_price_check", {"customer": "Alder Street Cafe"})["lines"]], ["1088"])
        with self.assertRaisesRegex(ToolError, "from is after to"):
            call("qb_price_check", {"from": "2026-10-21", "to": "2026-10-01"})
        # A cash sale with no customer, and a delivery fee billed without a quantity exactly 5% below its price.
        sale = next(t for t in c.txns["SalesReceipt"] if t["TxnDate"] >= r["period"]["from"])
        del sale["CustomerRef"]
        sale["SalesReceiptLineRet"] = [{"TxnLineID": "ZZ-1", "ItemRef": {"FullName": "Primer 1gal"}, "Quantity": 2, "Rate": 12, "Amount": 24},
                                       {"TxnLineID": "ZZ-2", "ItemRef": {"FullName": "Delivery Fee"}, "Rate": 42.75, "Amount": 42.75}]
        cash = call("qb_price_check", {"min_gap_percent": 5})
        self.assertEqual([(x["item"], x["quantity"], x["gap"], "customer" in x) for x in cash["lines"] if x["ref"] == sale["RefNumber"]],
                         [("Primer 1gal", 2, 24, False), ("Delivery Fee", 1, 2.25, False)])
        self.assertEqual([x["customer"] for x in cash["by_customer"]], ["Cedar Point Marina", "Alder Street Cafe"])

    def test_price_check_uses_the_item_list_when_price_rules_turn_price_levels_off(self):
        ctx, call = demo()
        line = next(x for t in ctx.qb.transport.company.txns["Invoice"] if t["RefNumber"] == "1088"
                    for x in t["InvoiceLineRet"] if x["ItemRef"]["FullName"] == "Exterior Paint 1gal White")
        line.update(Rate=30, Amount=round(30 * line["Quantity"], 2))
        handle = ctx.qb.transport._handle

        def price_rules_on(message, rq):
            # What Enterprise answered on the test VM when Advanced Pricing was on.
            if message == "CustomerQueryRq" and "PriceLevelRef" in str(rq.get("IncludeRetElement")):
                raise Status(3220, "There is no permission to perform this request, or the feature has been turned off in "
                                   "QuickBooks. Price Level is switched off because Price Rule was switched on")
            return handle(message, rq)

        with mock.patch.object(ctx.qb.transport, "_handle", price_rules_on):
            r = call("qb_price_check")
        x = next(x for x in r["lines"] if x["ref"] == "1088")
        self.assertEqual((x["expected"], "price_level" in x), (38, False))
        self.assertIn("Price levels are off", r["notes"][0])

    def test_reconcile_check_finds_what_moved_the_beginning_balance(self):
        ctx, call = demo()
        args = {"account": "Business Checking", "statement_date": "2026-09-30", "reconciled_on": "2026-10-03"}
        ok = call("qb_reconcile_check", {**args, "bank_balance": "-32,668.90"})
        self.assertEqual((ok["reconciled_in_quickbooks"], ok["difference"], ok["reconciled_transactions"]), (-32668.9, 0, 18))
        self.assertRegex(ok["notes"][0], "^QuickBooks' reconciled total matches the bank's balance on 2026-09-30")
        self.assertNotIn("next_step", ok)
        # The reconciled rent check edited after the reconciliation, and a deposit deleted.
        rent = next(t for t in ctx.qb.transport.company.txns["Check"] if t["RefNumber"] == "5276")
        rent.update(Amount=3000.0, TimeModified="2026-10-05T09:12:00")
        ctx.qb.transport.deleted["TxnDeletedRet"].append(
            {"TxnDelType": "Deposit", "TxnID": "ZZ-1", "TimeCreated": "2026-09-20T10:00:00", "TimeDeleted": "2026-10-06T16:40:00", "RefNumber": "DEP-77"})
        off = call("qb_reconcile_check", {**args, "bank_balance": "-32668.90"})
        self.assertEqual(off["difference"], 200)
        self.assertIn("200.00 more in QuickBooks", off["notes"][0])
        self.assertRegex(off["next_step"], "^Fix each changed, voided or deleted transaction")
        self.assertEqual(off["changed_since"]["transactions"], [{"date": "2026-09-01", "type": "Check", "ref": "5276", "name": "Riverbend Properties",
                                                                 "amount": -3000, "modified": "2026-10-05", "by": "Admin", "id": rent["TxnID"]}])
        self.assertEqual(off["deleted_since"], [{"type": "Deposit", "ref": "DEP-77", "deleted": "2026-10-06T16:40:00"}])
        self.assertEqual(call("qb_reconcile_check", {**args, "reconciled_on": "2026-10-06", "bank_balance": "0"})["changed_since"]["transactions"], [])
        with self.assertRaisesRegex(ToolError, "is not a bank account"):
            call("qb_reconcile_check", {**args, "account": "Business Visa", "bank_balance": "0"})
        with self.assertRaisesRegex(ToolError, "is not an amount"):
            call("qb_reconcile_check", {**args, "bank_balance": "about 300"})
        self.assertEqual((report_day("10/5/2026 9:12:00 AM"), report_day("2026-10-05T09:12:00"), report_day("")), ("2026-10-05", "2026-10-05", ""))
        register = ctx.qb.transport._register
        ctx.qb.transport._register = lambda rq, rng: register({**rq, "IncludeColumn": [x for x in rq["IncludeColumn"] if x != "ModifiedTime"]}, rng)
        blind = call("qb_reconcile_check", {**args, "bank_balance": "0"})
        self.assertEqual(blind["changed_since"]["transactions"], [])
        self.assertIn("without its Last modified column", " ".join(blind["notes"]))

    def test_sales_tax_check_compares_the_liability_report_with_the_books_and_finds_payments_around_pay_sales_tax(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        self.assertEqual((call("qb_sales_tax_check")["difference"], call("qb_sales_tax_check")["paid_outside_pay_sales_tax"]["transactions"]), (0, []))
        next(t for t in c.txns["Invoice"] if t["TxnDate"] >= "2026-10-01")["SalesTaxTotal"] = 80.0
        c.txns["Check"].append({"TxnID": "ZZ-CHK", "TxnDate": "2026-10-05", "RefNumber": "5301", "TimeModified": "2026-10-05T10:00:00",
                                "PayeeEntityRef": {"FullName": "State Tax Agency"}, "BankAccountRef": {"FullName": "Business Checking"}, "Amount": 50.0,
                                "Memo": "Q3 sales tax", "ExpenseLineRet": [{"TxnLineID": "ZZ-1", "AccountRef": {"FullName": "Sales Tax Payable"}, "Amount": 50.0}]})

        def journal(txn_id: str, amount: float, agency: str | None) -> dict:
            debit = {"TxnLineID": f"{txn_id}-1", "AccountRef": {"FullName": "Sales Tax Payable"}, "Amount": amount, **({"EntityRef": {"FullName": agency}} if agency else {})}
            return {"TxnID": txn_id, "TxnDate": "2026-10-06", "RefNumber": txn_id, "TimeModified": "2026-10-06T10:00:00", "JournalDebitLine": [debit],
                    "JournalCreditLine": [{"TxnLineID": f"{txn_id}-2", "AccountRef": {"FullName": "Uncategorized Expenses"}, "Amount": amount}]}

        c.txns["JournalEntry"] = [journal("ADJ-1", 10.0, None), journal("ADJ-2", 5.0, "State Tax Agency")]
        stp = next(a for a in c.lists["Account"] if a["Name"] == "Sales Tax Payable")
        stp["Balance"] = 15.0  # 80 collected, less the check and both journal entries
        r = call("qb_sales_tax_check")
        self.assertEqual((r["owed_per_liability_report"], r["balance_sheet"], r["difference"]), (80, {"accrual": 15, "cash": 15}, -65))
        self.assertEqual(r["paid_outside_pay_sales_tax"]["transactions"], [{"date": "2026-10-05", "type": "Check", "ref": "5301", "name": "State Tax Agency",
                                                                            "memo": "Q3 sales tax", "amount": 50, "id": "ZZ-CHK"}])
        self.assertEqual([x["ref"] for x in r["journal_entries_without_vendor"]], ["ADJ-1"])
        self.assertIn("80.00 is owed on 2026-10-20; Sales Tax Payable on the accrual balance sheet is 15.00, 65.00 less", r["notes"][0])
        self.assertIn("Pay Sales Tax with the same check number", r["notes"][1])
        self.assertIn("You must specify a vendor name", r["notes"][2])
        self.assertEqual(call("qb_sales_tax_check", {"from": "2026-10-06"})["paid_outside_pay_sales_tax"]["transactions"], [])

        sheet = ctx.qb.transport._balance_sheet

        def by_basis(rng: dict, basis: str) -> dict:
            stp["Balance"] = 80.0 if basis == "Cash" else 15.0
            return sheet(rng, basis)

        ctx.qb.transport._balance_sheet = by_basis
        self.assertIn("agrees with the cash-basis balance sheet, not the accrual one", call("qb_sales_tax_check")["notes"][0])
        del stp["SpecialAccountType"]
        self.assertEqual(call("qb_sales_tax_check")["sales_tax_accounts"], [])
        with self.assertRaisesRegex(ToolError, "from is after as_of"):
            call("qb_sales_tax_check", {"from": "2026-10-21", "as_of": "2026-10-01"})

    def test_currency_values_foreign_balances_and_finds_left_overs_and_odd_rates(self):
        ctx, call = demo()
        t = ctx.qb.transport
        c = t.company
        self.assertEqual(call("qb_currency"), {"multi_currency": False, "notes": ["Multicurrency is off in this company file, so every amount is in one currency."]})
        c.home_currency = "US Dollar"
        self.assertEqual(call("qb_currency")["notes"], ["No account holds a currency other than US Dollar yet."])

        cad = {"FullName": "Canadian Dollar"}
        c.lists["Currency"] = [{"ListID": "CUR-1", "Name": "US Dollar", "IsActive": True, "CurrencyCode": "USD", "ExchangeRate": 1.0, "AsOfDate": "2026-10-20"},
                               {"ListID": "CUR-2", "Name": "Canadian Dollar", "IsActive": True, "CurrencyCode": "CAD", "ExchangeRate": 0.73, "AsOfDate": "2026-08-31"}]
        for name, type_, balance in (("Accounts Receivable - CAD", "AccountsReceivable", 1000.0), ("Accounts Payable - CAD", "AccountsPayable", 400.0),
                                     ("Chequing CAD", "Bank", 0.0), ("Savings CAD", "Bank", 0.0)):
            c.lists["Account"].append({"ListID": f"ZZ-{name}", "Name": name, "FullName": name, "IsActive": True, "Sublevel": 0, "AccountType": type_,
                                       "Balance": balance, "TotalBalance": balance, "CurrencyRef": cad})
        for base, name, balance in (("Customer", "Maple Supply", 0.0), ("Customer", "Lakeshore Co", 1000.0), ("Vendor", "Northern Mills", 0.0)):
            # Like QuickBooks' VendorRet, a vendor has no FullName.
            c.lists[base].append({"ListID": f"ZZ-{name}", "Name": name, **({} if base == "Vendor" else {"FullName": name, "Sublevel": 0, "TotalBalance": balance}),
                                  "IsActive": True, "Balance": balance, "CurrencyRef": cad})
        for i, rate in enumerate((0.74, 0.75, 0.73, 0.74, 0.75, 1.0, 1.35), 1):
            c.txns["Invoice"].append({"TxnID": f"ZZ-FX{i}", "TxnDate": f"2026-10-0{i}", "RefNumber": f"FX-{i}", "TimeModified": f"2026-10-0{i}T10:00:00",
                                      "CustomerRef": {"FullName": "Lakeshore Co"}, "Subtotal": 100.0, "CurrencyRef": cad, "ExchangeRate": rate})
        # What the books carry in US dollars, as QuickBooks' reports show it. Maple Supply and Northern Mills were paid in full
        # but kept 8.40 and 3.20.
        homes = {"BalanceSheetStandard": [("Accounts Receivable - CAD", 750.0), ("Accounts Payable - CAD", 300.0), ("Chequing CAD", 12.5)],
                 "CustomerBalanceSummary": [("Lakeshore Co", 741.6), ("Maple Supply", 8.4)], "VendorBalanceSummary": [("Northern Mills", 3.2)]}
        report = t._report

        def in_home(message: str, rq: dict) -> dict:
            kind = rq.get("GeneralSummaryReportType")
            if kind not in homes:
                return report(message, rq)
            rows = [{"kind": "data", "label": n, "rowType": "account", "values": [v]} for n, v in homes[kind]]
            return t._report_xml(kind, {"from": c.today, "to": c.today}, None, [("", "Blank", "STRTYPE"), ("Balance", "Amount", "AMTTYPE")], rows)

        t._report = in_home
        r = call("qb_currency")
        cur = r["currencies"][0]
        self.assertEqual({k: cur[k] for k in ("currency", "code", "rate", "rate_from", "rate_set_on", "gain")},
                         {"currency": "Canadian Dollar", "code": "CAD", "rate": 0.73, "rate_from": "QuickBooks", "rate_set_on": "2026-08-31", "gain": -24.5})
        self.assertEqual([(x["account"], x["balance"], x["carrying_value"], x["at_rate"], x["gain"]) for x in cur["accounts"]],
                         [("Accounts Receivable - CAD", 1000, 750, 730, -20), ("Accounts Payable - CAD", 400, 300, 292, 8), ("Chequing CAD", 0, 12.5, 0, -12.5)])
        self.assertEqual(cur["left_over"], [{"kind": "account", "name": "Chequing CAD", "carrying_value": 12.5},
                                            {"kind": "customer", "name": "Maple Supply", "carrying_value": 8.4},
                                            {"kind": "vendor", "name": "Northern Mills", "carrying_value": 3.2}])
        self.assertEqual((cur["transactions_checked"], cur["usual_rate"]), (7, 0.745))
        self.assertEqual([(u["ref"], u["rate"], u["home_amount"], u["at_usual_rate"], u["hint"]) for u in cur["unusual_rates"]],
                         [("FX-7", 1.35, 135, 74.5, "upside_down"), ("FX-6", 1.0, 100, 74.5, "rate_is_1")])
        self.assertIn("CAD rate (0.73) was set on 2026-08-31, 50 days ago", r["notes"][0])
        self.assertIn("(Lists > Currency List > Activities > Download Latest Exchange Rates)", r["notes"][0])
        self.assertIn("show an unrealized exchange loss of 24.50 US Dollar against what the books carry", r["notes"][1])
        self.assertIn("there is none in the last 400 days", r["notes"][1])
        self.assertIs(r["last_adjustment"], MISSING)
        self.assertIn("A home currency adjustment ignores foreign balances of zero", r["notes"][2])
        self.assertIn("rate_is_1 usually means no rate was set", r["notes"][3])

        c.txns["JournalEntry"] = [{"TxnID": f"ZZ-HCA{i}", "TxnDate": d, "RefNumber": f"HCA-{i}", "TimeModified": f"{d}T10:00:00", "IsHomeCurrencyAdjustment": hca}
                                  for i, (d, hca) in enumerate((("2026-06-30", True), ("2026-09-30", False), ("2025-03-31", True)), 1)]
        self.assertEqual(call("qb_currency")["last_adjustment"], {"date": "2026-06-30", "ref": "HCA-1"})
        given = call("qb_currency", {"rates": ["cad=0.80"]})
        cur = given["currencies"][0]
        self.assertEqual((cur["rate"], cur["rate_from"], "rate_set_on" in cur, cur["gain"]), (0.8, "given", False, 17.5))
        self.assertIn("show an unrealized exchange gain of 17.50 US Dollar against what the books carry", given["notes"][0])
        self.assertIn("No foreign account holds EUR", call("qb_currency", {"rates": ["EUR=1.1"]})["notes"][-1])
        for bad in ("CAD", "CAD=-1", "=1.2"):
            with self.assertRaisesRegex(ToolError, "is not CODE=RATE"):
                call("qb_currency", {"rates": [bad]})
        few = call("qb_currency", {"limit": 1})["currencies"][0]
        self.assertEqual((len(few["left_over"]), few["left_over_more"], len(few["unusual_rates"]), few["unusual_rates_more"]), (1, 2, 1, 1))
        # QuickBooks downloads rates only for a US dollar home currency.
        c.home_currency = "Euro"
        c.lists["Currency"].append({"ListID": "CUR-3", "Name": "Euro", "IsActive": True, "CurrencyCode": "EUR", "ExchangeRate": 1.0, "AsOfDate": "2026-10-20"})
        self.assertIn("(Lists > Currency List, open the currency and enter the rate for the date)", call("qb_currency")["notes"][0])
        c.home_currency = "US Dollar"
        c.lists["Currency"][1]["ExchangeRate"] = 0.0
        unset = call("qb_currency")
        self.assertEqual((unset["currencies"][0]["rate"], unset["currencies"][0]["gain"], "at_rate" in unset["currencies"][0]["accounts"][0]), (MISSING, MISSING, False))
        self.assertIn("QuickBooks has no CAD rate: pass --rates CAD=<rate>", unset["notes"][0])
        self.assertEqual(unset["currencies"][0]["usual_rate"], 0.745)
        # Rates left at 1 do not become the usual rate, however many there are.
        for i in range(8, 13):
            c.txns["Invoice"].append({**c.txns["Invoice"][-1], "TxnID": f"ZZ-FX{i}", "RefNumber": f"FX-{i}", "ExchangeRate": 1.0})
        ones = call("qb_currency")["currencies"][0]
        self.assertEqual((ones["usual_rate"], sum(u.get("hint") == "rate_is_1" for u in ones["unusual_rates"])), (0.745, 6))

    def test_uncleared_lists_old_items_with_a_likely_twin_and_unfinished_reconciliations(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        stale = next(t for t in c.txns["Check"] if c.cleared.get(t["TxnID"]) == "NotCleared")
        u = call("qb_uncleared")
        self.assertEqual([x["id"] for x in u["uncleared"]], [stale["TxnID"]])
        self.assertGreaterEqual(u["uncleared"][0]["days"], 30)
        self.assertEqual([x["id"] for x in u["pending"]], [t for t, s in c.cleared.items() if s == "Pending"])
        checking = next(x for x in u["accounts"] if x["account"] == "Business Checking")
        self.assertEqual((checking["uncleared"], checking["uncleared_total"], checking["pending"]), (1, -stale["Amount"], 1))

        # The same rent check entered twice: the bank clears one, the other never clears.
        rent = next(t for t in c.txns["Check"] if t.get("Memo") == "Monthly rent" and t["TxnDate"] <= "2026-08-31")
        c.txns["Check"].append({**rent, "TxnID": "ZZ-TWIN", "RefNumber": "9999"})
        c.cleared["ZZ-TWIN"] = "NotCleared"
        twin = next(x for x in call("qb_uncleared")["uncleared"] if x["id"] == "ZZ-TWIN")
        self.assertEqual((twin["twin"]["id"], twin["twin"]["status"]), (rent["TxnID"], "cleared"))

        october_rent = next(t for t in c.txns["Check"] if t["TxnDate"] == "2026-10-01")
        c.cleared[october_rent["TxnID"]] = "NotCleared"
        self.assertNotIn(october_rent["TxnID"], [x["id"] for x in call("qb_uncleared")["uncleared"]])
        self.assertIn(october_rent["TxnID"], [x["id"] for x in call("qb_uncleared", {"older_than_days": 10})["uncleared"]])
        visa = call("qb_uncleared", {"account": "business visa"})
        self.assertEqual([x["account"] for x in visa["accounts"]], ["Business Visa"])
        with self.assertRaisesRegex(ToolError, "not a bank or credit card account"):
            call("qb_uncleared", {"account": "Rent Expense"})

        # Without the Clr column every item would look uncleared.
        report = ctx.qb.report

        def without_clr(message: str, body: dict) -> Report:
            r = report(message, body)
            r.columns = [ReportColumn("", "", "Blank", "") if col.type == "ClearedStatus" else col for col in r.columns]
            return r
        ctx.qb.report = without_clr
        with self.assertRaisesRegex(ToolError, "without its Clr column"):
            call("qb_uncleared")

    def test_deposits_find_the_payments_a_bank_deposit_holds(self):
        _, call = demo(QBD_WRITE_ENTITIES="deposit")
        d = call("qb_deposits")
        self.assertEqual((d["waiting"]["payments"], d["waiting"]["total"]), (8, 13122.56))
        self.assertEqual({m["method"]: m["payments"] for m in d["by_method"]}, {"Check": 5, "ACH": 3})
        checks, ach = (next(b for b in d["by_day"] if b["method"] == m and b["payments"] > 1) for m in ("Check", "ACH"))

        batch = call("qb_deposits", {"amount": str(checks["total"]), "account": "Business Checking"})
        self.assertEqual((batch["match"]["how"], batch["match"]["confidence"]), (f"the Check payments of {checks['date']}", "high"))
        lines = batch["draft_fields"]["DepositLineAdd"]
        self.assertEqual(len(lines), 3)
        p = call("qb_draft", {"action": "add", "type": "deposit", "fields": batch["draft_fields"]})
        self.assertIn(f"total {checks['total']:,.2f}", p["preview"])

        fee = call("qb_deposits", {"amount": f"{ach['total'] - 31.95:.2f}"})
        self.assertEqual((fee["match"]["fee"], fee["match"]["confidence"]), (31.95, "medium"))
        self.assertIn('"Amount": -31.95}', fee["next_step"])
        self.assertIn("adding DepositToAccountRef", fee["next_step"])
        self.assertEqual(call("qb_deposits", {"amount": "1,075.92"})["match"]["how"], "2 payments that add up exactly")
        none = call("qb_deposits", {"amount": "99.99"})
        self.assertEqual(none["match"]["confidence"], "none")
        self.assertNotIn("draft_fields", none)
        with self.assertRaisesRegex(ToolError, "not an amount"):
            call("qb_deposits", {"amount": "lots"})

    def test_trace_follows_an_order_to_its_invoice_and_a_payment_to_the_bank(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        waiting = next(p for p in c.txns["ReceivePayment"] if p["TxnID"] in {u["TxnID"] for u in c.undeposited})
        t = call("qb_trace", {"ref": waiting["AppliedToTxnRet"][0]["RefNumber"], "type": "invoice"})
        self.assertEqual((t["start"]["type"], t["start"]["state"]), ("Invoice", "paid"))
        self.assertEqual([(x["type"], x["state"]) for x in t["chain"]], [("ReceivePayment", "waiting in Undeposited Funds")])
        self.assertEqual(t["loose_ends"], [f"ReceivePayment {waiting['RefNumber']}: not deposited yet"])

        # A sales order invoiced with LinkToTxnID: traced forward from the order and back from the invoice.
        so = next(x for x in c.txns["SalesOrder"] if x["RefNumber"] == "SO-881")
        line = so["SalesOrderLineRet"][0]
        d = call("qb_draft", {"action": "add", "type": "invoice", "fields": {
            "CustomerRef": so["CustomerRef"]["FullName"], "TxnDate": TODAY, "LinkToTxnID": so["TxnID"],
            "InvoiceLineAdd": [{"ItemRef": line["ItemRef"]["FullName"], "Quantity": line["Quantity"], "Rate": line["Rate"]}]}})
        invoice = call("qb_commit", {"token": d["token"]})["ids"]["RefNumber"]
        forward = call("qb_trace", {"ref": "SO-881"})
        self.assertEqual([(x["type"], x["ref"], x["via"]) for x in forward["chain"]], [("Invoice", invoice, "SalesOrder SO-881")])
        self.assertIn("SalesOrder SO-881: not fully invoiced", forward["loose_ends"])
        self.assertEqual([x["ref"] for x in call("qb_trace", {"ref": invoice, "type": "invoice"})["chain"]], ["SO-881"])

        # One number, two transactions: the caller picks.
        c.txns["Check"].append({**c.txns["Check"][0], "TxnID": "ZZ-TRACE-1", "RefNumber": invoice})
        self.assertEqual(sorted(x["type"] for x in call("qb_trace", {"ref": invoice})["several"]), ["Check", "Invoice"])
        with self.assertRaisesRegex(ToolError, 'No transaction numbered "NOPE-1"'):
            call("qb_trace", {"ref": "NOPE-1"})

    def test_match_payment_finds_the_invoices_a_payment_pays_and_gives_the_draft_fields(self):
        ctx, call = demo()
        name = "Dunmore Property Management"
        docs = sorted((x for x in open_items(ctx, "AR", name) if x["open"] > 0), key=lambda x: x["date"])
        self.assertGreaterEqual(len(docs), 4)

        def match(amount: float, **more) -> dict:
            return call("qb_match_payment", {"customer": name, "amount": f"{amount:.2f}", **more})

        def refs(m: dict) -> list:
            return [x["ref"] for x in m["match"]["invoices"]]

        one = match(docs[2]["open"])
        self.assertEqual((refs(one), one["match"]["confidence"]), ([docs[2]["ref"]], "high"))
        self.assertEqual(one["draft_fields"]["AppliedToTxnAdd"], [{"TxnID": docs[2]["id"], "PaymentAmount": docs[2]["open"]}])
        two = match(docs[0]["open"] + docs[1]["open"])
        self.assertEqual((refs(two), two["match"]["how"]), ([docs[0]["ref"], docs[1]["ref"]], "the oldest 2 invoices"))
        whole = match(sum(x["open"] for x in docs))
        self.assertEqual((whole["match"]["how"], whole["match"]["confidence"]), ("the whole open balance", "high"))
        gap = match(docs[0]["open"] + docs[2]["open"])
        self.assertEqual((refs(gap), gap["match"]["how"]), ([docs[0]["ref"], docs[2]["ref"]], "2 invoices that add up exactly"))
        memo = match(docs[1]["open"] + docs[3]["open"], memo=f"Paying INV {docs[1]['ref']} and #{docs[3]['ref']}")
        self.assertEqual((refs(memo), memo["match"]["confidence"]), ([docs[1]["ref"], docs[3]["ref"]], "high"))
        over = match(sum(x["open"] for x in docs) + 100.5)
        self.assertEqual((over["match"]["confidence"], over["match"]["left_over"]), ("low", 100.5))
        self.assertIn("nearest", over)

        # The fields go straight into a draft: those two invoices are then paid.
        d = call("qb_draft", {"action": "add", "type": "receive_payment", "fields": two["draft_fields"]})
        call("qb_commit", {"token": d["token"]})
        self.assertEqual([x["ref"] for x in open_items(ctx, "AR", name) if x["open"] > 0 and x["ref"] in refs(two)], [])
        with self.assertRaisesRegex(ToolError, "not an amount"):
            call("qb_match_payment", {"customer": name, "amount": "twelve"})
        with self.assertRaisesRegex(ToolError, 'No customer named "Dunmore".*Dunmore Property Management'):
            call("qb_match_payment", {"customer": "Dunmore", "amount": "10"})

    def test_coding_shows_where_a_vendor_usually_goes_and_what_was_coded_otherwise(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        b = call("qb_coding", {"vendor": "brightway office products"})
        self.assertEqual((b["vendor"], b["read"]), ("Brightway Office Products", {"bills": 2, "checks": 1, "card_charges": 3}))
        self.assertEqual(b["prefill_accounts"], ["Office Supplies"])
        self.assertEqual(b["usually"], {"account": "Office Supplies", "transactions": 5, "of": 6})
        self.assertEqual([(x["account"], x["transactions"], x["share"]) for x in b["accounts"]], [("Office Supplies", 5, 0.83), ("Uncategorized Expenses", 1, 0.17)])
        stray = next(t for t in c.txns["Check"] if t["PayeeEntityRef"]["FullName"] == "Brightway Office Products")
        self.assertEqual([(x["id"], x["account"]) for x in b["check"]], [(stray["TxnID"], "Uncategorized Expenses")])
        self.assertIn("usually goes to Office Supplies", b["check"][0]["why"])
        self.assertNotIn("notes", b)
        self.assertEqual(call("qb_coding", {"vendor": "Riverbend Properties"})["accounts"][0]["memos"], ["Monthly rent"])

        # Stock purchases are coded to items: their last cost is the evidence for a new bill's prices.
        s = call("qb_coding", {"vendor": "Summit Lumber Supply"})
        self.assertEqual(s["accounts"], [])
        self.assertNotIn("usually", s)
        costs = {i["FullName"]: i["PurchaseCost"] for i in c.lists["ItemInventory"]}
        self.assertTrue(s["items"] and all(x["last_cost"] == costs[x["item"]] and x["quantity"] > 0 for x in s["items"]))

        # A prefill account nobody uses any more, and a read cut short.
        next(v for v in c.lists["Vendor"] if v["Name"] == "Brightway Office Products")["PrefillAccountRef"] = [{"FullName": "Advertising and Promotion"}]
        notes = call("qb_coding", {"vendor": "Brightway Office Products", "limit": 1})["notes"]
        self.assertIn("pre-fills Advertising and Promotion", notes[0])
        self.assertIn("Only the first 1 bills and card charges", notes[1])
        with self.assertRaisesRegex(ToolError, 'No vendor named "Brightway".*Brightway Office Products'):
            call("qb_coding", {"vendor": "Brightway"})

    def test_purchasing_lists_late_lines_vendor_totals_and_goods_received_not_billed(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        p = call("qb_purchasing")
        self.assertEqual(p["open_orders"]["orders"], 2, "the two fully received orders are left out")
        self.assertEqual([(x["po"], x["item"]) for x in p["late"]], [("PO-1468", "Outdoor Outlet Cover")])
        self.assertGreater(p["late"][0]["days_late"], 60)
        self.assertEqual((p["by_vendor"][0]["vendor"], p["by_vendor"][0]["late_lines"]), ("Metro Electric Wholesale", 1))
        self.assertNotIn("late_lines", p["by_vendor"][1])
        # Metro Electric billed the covers twice after PO-1468 without linking the bills to it, so the order looks late.
        tw = p["three_way_match"]
        self.assertEqual([(u["po"], u["bills"], u["billed"]) for u in tw["bills_not_linked"]], [("PO-1468", ["INV-7010", "INV-7002"], 73)])
        self.assertEqual(p["late"][0]["possibly_billed_on"], ["INV-7010", "INV-7002"])
        self.assertEqual((tw["cost_differences"], tw["billed_beyond_ordered"]), ([], []))
        c.txns["Bill"].append({"TxnID": "ZZ-BILL-1", "TxnDate": "2026-10-16", "RefNumber": "INV-7020", "VendorRef": {"FullName": "Pioneer Paint Distributors"},
                               "LinkToTxnID": ["10D0-1700000000"], "AmountDue": 922.5,
                               "ItemLineRet": [{"TxnLineID": "ZZ-1", "ItemRef": {"FullName": "Wood Stain 1gal Cedar"}, "Quantity": 45, "Cost": 20.5, "Amount": 922.5}]})
        tw = call("qb_purchasing", {"vendor": "Pioneer Paint Distributors"})["three_way_match"]
        self.assertEqual(tw["bills_linked_to_orders"], 1)
        self.assertEqual([(x["bill"], x["po"], x["po_rate"], x["bill_cost"], x["difference"]) for x in tw["cost_differences"]], [("INV-7020", "PO-1501", 19.8, 20.5, 31.5)])
        self.assertEqual([(x["po"], x["ordered"], x["billed"], x["bills"]) for x in tw["billed_beyond_ordered"]], [("PO-1501", 40, 45, ["INV-7020"])])
        self.assertEqual(tw["bills_not_linked"], [])
        c.txns.setdefault("ItemReceipt", []).append({"TxnID": "ZZ-IR-1", "TxnDate": "2026-09-30", "RefNumber": "IR-77",
                                                     "VendorRef": {"FullName": "Summit Lumber Supply"}, "TotalAmount": 512.4})
        summit = call("qb_purchasing", {"vendor": "Summit Lumber Supply"})
        self.assertEqual(summit["open_orders"]["orders"], 0)
        rnb = summit["received_not_billed"]
        self.assertEqual((rnb["receipts"], rnb["total"], rnb["oldest_first"][0]["receipt"], rnb["oldest_first"][0]["days"]), (1, 512.4, "IR-77", 20))

    def test_stock_history_shows_each_time_an_item_went_below_zero_and_what_ended_it(self):
        ctx, call = demo()
        sent: list[str] = []
        process = ctx.qb.transport.process
        ctx.qb.transport.process = lambda xml: (sent.append(xml), process(xml))[1]
        h = call("qb_stock_history", {"item": "led flood light 50w"})
        rq = next(x for x in sent if "InventoryValuationDetail" in x)
        self.assertIn("<ReportItemFilter><FullName>LED Flood Light 50W</FullName></ReportItemFilter>", rq)
        self.assertIn("<IncludeColumn>QuantityOnHand</IncludeColumn>", rq)
        self.assertEqual(h["period"], {"from": "2025-10-20", "to": TODAY})
        self.assertEqual(h["on_hand"], {"at_start": 93, "at_end": -3, "item_list": -3})
        self.assertEqual((h["movements"], h["in"], h["out"]), (18, 98, -194))
        first, now = h["below_zero"]
        self.assertEqual((first["from"], first["started_by"]["ref"], first["started_by"]["on_hand_before"], first["started_by"]["on_hand_after"]), ("2026-07-27", "1068", 17, -5))
        self.assertEqual((first["lowest"], first["days"], first["ended_by"]["type"], first["ended_by"]["ref"], first["ended_by"]["on_hand_after"]), (-67, 63, "Bill", "INV-7006", 18))
        self.assertEqual((now["from"], now["started_by"]["ref"], now["lowest"], now["days"], now["still_below_zero"]), ("2026-10-13", "1062", -3, 7, True))
        self.assertIn("date that purchase on or before the sale", h["next_step"])
        self.assertIn("still below zero", h["next_step"])
        later = call("qb_stock_history", {"item": "LED Flood Light 50W", "from": "2026-08-01", "limit": 2})
        self.assertEqual((later["below_zero"][0]["started_before_period"], later["below_zero"][0]["lowest"]), (True, -67))
        self.assertEqual((len(later["moves"]), later["more_moves"]), (2, 7))
        self.assertIn("run again with an earlier from", later["next_step"])
        # Below zero for 40 days in the summer and back above since: QuickBooks' Negative Item Listing would not show it.
        hinge = call("qb_stock_history", {"item": "Hinge Set Heavy Duty"})
        self.assertEqual((hinge["on_hand"]["at_end"], len(hinge["below_zero"]), hinge["below_zero"][0]["days"]), (64, 1, 40))
        self.assertNotIn("still below zero", hinge["next_step"])
        # A sale dated ahead is in the item list's quantity, not in the report up to today.
        ctx.qb.transport.company.txns["Invoice"].append({"TxnID": "ZZ-INV-1", "TxnNumber": 9999, "TxnDate": "2026-11-02", "RefNumber": "1200", "CustomerRef": {"FullName": "Alder Street Cafe"},
                                                         "InvoiceLineRet": [{"TxnLineID": "ZZ-1", "ItemRef": {"FullName": "LED Flood Light 50W"}, "Quantity": 5, "Rate": 54.99, "Amount": 274.95}]})
        self.assertEqual(call("qb_stock_history", {"item": "LED Flood Light 50W"})["notes"],
                         ["The item list shows -3 on hand and the report 2 on 2026-10-20: look for transactions dated after 2026-10-20."])
        self.assertEqual(call("qb_stock_history", {"item": "LED Flood Light 50W", "to": "2026-01-31"})["notes"], ["No stock movements between 2025-01-31 and 2026-01-31."])
        with self.assertRaisesRegex(ToolError, "not an inventory or assembly item"):
            call("qb_stock_history", {"item": "Installation Labor"})
        with self.assertRaisesRegex(ToolError, 'No item named "LED Flood". Items with "LED" in their name: LED Flood Light 50W.'):
            call("qb_stock_history", {"item": "LED Flood"})
        with self.assertRaisesRegex(ToolError, "One year at a time"):
            call("qb_stock_history", {"item": "LED Flood Light 50W", "from": "2025-01-01"})

    def test_1099_worklist_counts_mapped_accounts_by_check_against_the_years_threshold(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        r = call("qb_1099")
        self.assertEqual((r["year"], r["so_far"], r["irs_threshold"]), (2026, True, 2000))
        self.assertEqual(r["mapping"], [{"box": "1099-MISC Box 1: Rents", "accounts": ["Rent Expense"]}])
        rent = r["send_1099"][0]
        self.assertEqual((rent["vendor"], rent["counted"], rent["tax_id"], rent["address"]), ("Riverbend Properties", 16000, "not shared", "missing PostalCode"))
        self.assertIn("uses 600.00 for 1099-MISC Box 1: Rents; for payments made in 2026 the IRS threshold is 2,000.00", " ".join(r["notes"]))
        self.assertEqual(r["check_these"], [])
        c.personal_data = True
        self.assertEqual(call("qb_1099")["summary"]["missing_tax_id"], 1)
        self.assertEqual(call("qb_1099", {"year": 2025})["under_threshold"], ["Riverbend Properties"])
        self.assertEqual(call("qb_1099", {"year": 2025})["irs_threshold"], 600)
        # Bill payments count through the paid bills' lines; item lines through the item's account.
        c.form1099[0]["CategoryAccountMapping"].append({"MappingCategory": "Box 3: Other income", "AccountRef": [{"FullName": "Inventory Asset"}], "Threshold": 600})
        over = call("qb_1099")["check_these"]
        self.assertEqual([(x["vendor"], x["counted"]) for x in over], [("Pioneer Paint Distributors", 6024.4), ("Metro Electric Wholesale", 4821.6)])
        self.assertIn("not marked for 1099", over[0]["why"])

    def test_reorder_list_uses_on_order_stock_and_groups_by_vendor(self):
        _, call = demo()
        r = call("qb_inventory_reorder")
        self.assertEqual(r["sales_history"], {"from": "2026-07-22", "to": TODAY}, "the Qty column is found under its period title")
        items = [i["item"] for v in r["reorder"] for i in v["items"]]
        self.assertIn("Deck Screws 5lb", items)
        self.assertNotIn("Wood Stain 1gal Cedar", items, "40 on order covers it")
        for v in r["reorder"]:
            for i in v["items"]:
                self.assertTrue(i["days_of_cover"] is None or i["days_of_cover"] >= 0)


class Records(unittest.TestCase):
    def test_status_says_when_intuit_stops_supporting_this_quickbooks_version(self):
        _, call = demo()
        self.assertEqual(call("qb_status")["intuit_support"], {"version_year": 2024, "ends": "2027-05-31", "days_left": 223, "announced": False})
        self.assertNotIn("warnings", call("qb_status"))

        def host(version: str, country: str = "US") -> HostInfo:
            return HostInfo("QuickBooks Enterprise Solutions", version, country, ["16.0"], "16.0", MISSING, "")

        info, warnings = intuit_support(host("33.0"), TODAY)
        self.assertEqual(info, {"version_year": 2023, "ended": "2026-05-31"})
        self.assertRegex(warnings[0], "^Intuit stopped supporting QuickBooks Desktop 2023 after 2026-05-31: no live support")
        info, warnings = intuit_support(host("34.0"), "2027-03-01")
        self.assertEqual(info["days_left"], 91)
        self.assertRegex(warnings[0], r"is expected to stop supporting QuickBooks Desktop 2024 after 2027-05-31 \(in 91 days\)")
        info, warnings = intuit_support(host("33.0"), "2026-05-31")
        self.assertEqual(info["days_left"], 0)
        self.assertIn("after 2026-05-31 (today is the last day)", warnings[0])
        self.assertEqual(intuit_support(host("34.0", "CA"), TODAY), (MISSING, []), "dates are Intuit's US ones")
        self.assertEqual(intuit_support(host("?.?"), TODAY), (MISSING, []))

    def test_snapshot_keeps_balances_and_counts_and_compare_lists_what_moved(self):
        _, call = demo()
        before = call("qb_snapshot")
        self.assertEqual((before["snapshot"], before["accounts"]["Accounts Receivable"]), (1, 21561.48))
        self.assertEqual((before["counts"]["invoice"], before["counts"]["customer"], before["counts"]["account"]), (71, 14, 21))
        self.assertRegex(before["taken"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        with tempfile.TemporaryDirectory() as folder:
            def saved(name: str, value) -> str:
                path = os.path.join(folder, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(dumps(value))
                return path

            path = saved("before.json", before)
            self.assertEqual(call("qb_snapshot", {"compare": path})["notes"], ["Every balance and count is the same."])
            d = call("qb_draft", {"action": "add", "type": "invoice", "fields": {
                "CustomerRef": "Alder Street Cafe", "TxnDate": TODAY, "InvoiceLineAdd": [{"ItemRef": "Installation Labor", "Quantity": 2, "Rate": 150}]}})
            call("qb_commit", {"token": d["token"]})
            after = call("qb_snapshot", {"compare": path})
            self.assertIs(after["same"], False)
            self.assertEqual(after["balances"], [{"account": "Accounts Receivable", "before": 21561.48, "after": 21861.48, "change": 300}])
            self.assertEqual(after["counts"], [{"type": "invoice", "before": 71, "after": 72, "change": 1}])
            self.assertRegex(after["notes"][0], "^Balances changed")
            self.assertEqual(after["next_step"], f"qbd changes {before['taken']} lists what was added, changed or deleted since the first snapshot.")
            gone = {**before, "accounts": {**before["accounts"], "Old Clearing": 12.5}, "quickbooks": "QuickBooks Desktop 33.0"}
            moved = call("qb_snapshot", {"compare": saved("old.json", gone)})
            self.assertEqual(moved["balances"][0], {"account": "Old Clearing", "before": 12.5, "after": MISSING})
            self.assertIn("QuickBooks was QuickBooks Desktop 33.0 and is now", moved["notes"][0])
            uncounted = {**before, "counts": {k: v for k, v in before["counts"].items() if k != "estimate"}, "not_counted": ["estimate"]}
            partial = call("qb_snapshot", {"compare": saved("partial.json", uncounted)})
            self.assertEqual([x["type"] for x in partial["counts"]], ["invoice"])
            self.assertIn("Counted in only one of the two snapshots, so not compared: estimate.", partial["notes"])
            with self.assertRaisesRegex(ToolError, "compare snapshots of the same company"):
                call("qb_snapshot", {"compare": saved("other.json", {**before, "company": "Another Company"})})
            with self.assertRaisesRegex(ToolError, "is not a snapshot"):
                call("qb_snapshot", {"compare": saved("list.json", [1, 2])})
            with self.assertRaisesRegex(ToolError, "^No file at"):
                call("qb_snapshot", {"compare": os.path.join(folder, "none.json")})

    def test_reports_come_back_as_indented_rows_with_numbers(self):
        _, call = demo()
        r = call("qb_report", {"report": "profit_and_loss", "period": "LastMonth"})
        self.assertEqual(r["period"], "2026-09-01 through 2026-09-30")
        net = next(x for x in r["rows"] if str(x[0]).strip() == "Net Income")
        self.assertIsInstance(net[1], (int, float))
        with self.assertRaisesRegex(ToolError, "Unknown report"):
            call("qb_report", {"report": "no_such_report"})
        with self.assertRaisesRegex(ToolError, "need a period"):
            call("qb_report", {"report": "general_ledger", "period": "All"})
        empty = call("qb_report", {"report": "budget_vs_actual", "fiscal_year": 2026})
        self.assertEqual(empty["rows"], [])
        self.assertIn("no budget for fiscal year 2026", empty["note"])
        self.assertNotIn("note", r)

    def test_report_periods_go_out_as_dates_and_inventory_valuation_takes_an_as_of_date(self):
        ctx, call = demo()
        sent: list[str] = []
        process = ctx.qb.transport.process
        ctx.qb.transport.process = lambda xml: (sent.append(xml), process(xml))[1]
        call("qb_report", {"report": "sales_by_item", "period": "ThisCalendarQuarterToDate"})
        rq = next(x for x in sent if "SalesByItemSummary" in x)
        self.assertIn("<FromReportDate>2026-10-01</FromReportDate><ToReportDate>2026-10-20</ToReportDate>", rq)
        self.assertNotIn("ReportDateMacro", rq)
        sent.clear()
        with self.assertRaisesRegex(ToolError, "one item at a time"):  # for every item at once it stopped QuickBooks
            call("qb_report", {"report": "inventory_valuation", "to": "2026-09-30"})
        self.assertEqual(sent, [])
        with self.assertRaisesRegex(Exception, "sample company has no"):
            call("qb_report", {"report": "InventoryValuationSummary", "to": "2026-09-30", "item": "LED Flood Light 50W"})
        self.assertIn("<FromReportDate>2026-09-30</FromReportDate><ToReportDate>2026-09-30</ToReportDate>", "".join(sent))
        self.assertIn("<ReportItemFilter><FullNameWithChildren>LED Flood Light 50W</FullNameWithChildren></ReportItemFilter>", "".join(sent))
        with self.assertRaisesRegex(ToolError, "Give from as well as to"):
            call("qb_report", {"report": "sales_by_item", "to": "2026-09-30"})

    def test_search_validates_filters_per_record_type(self):
        _, call = demo()
        r = call("qb_search", {"type": "invoice", "party": "Granite Peak Contractors", "status": "open"})
        self.assertTrue(r["rows"])
        self.assertTrue(all(x["Customer"] == "Granite Peak Contractors" and x["IsPaid"] is False for x in r["rows"]))
        with self.assertRaisesRegex(ToolError, "transaction filter"):
            call("qb_search", {"type": "customer", "status": "open"})
        with self.assertRaisesRegex(ToolError, "transactions have no name"):
            call("qb_search", {"type": "invoice", "name": "x"})
        with self.assertRaisesRegex(ToolError, "has no field Nope"):
            call("qb_search", {"type": "invoice", "fields": ["Nope"]})

    def test_raw_only_runs_queries(self):
        _, call = demo()
        with self.assertRaisesRegex(ToolError, "only runs queries"):
            call("qb_raw", {"request": "InvoiceAddRq", "body": {}})
        self.assertEqual(call("qb_raw", {"request": "PaymentMethodQuery"})["count"], 4)

    def test_count_asks_quickbooks_for_a_count_with_the_search_filters(self):
        ctx, call = demo()
        sent: list[str] = []
        process = ctx.qb.transport.process
        ctx.qb.transport.process = lambda xml: (sent.append(xml), process(xml))[1]
        everything = call("qb_search", {"type": "invoice", "limit": 1000})["count"]
        self.assertEqual(call("qb_count", {"type": "invoice"}), {"type": "invoice", "count": everything})
        self.assertIn('metaData="MetaDataOnly"', sent[-1])
        open_ = call("qb_search", {"type": "invoice", "status": "open", "limit": 1000})["count"]
        self.assertEqual(call("qb_count", {"type": "invoice", "status": "open"})["count"], open_)
        self.assertLess(open_, everything)
        self.assertGreaterEqual(call("qb_count", {"type": "customer", "active": "all"})["count"], call("qb_count", {"type": "customer"})["count"])
        with self.assertRaisesRegex(ToolError, "transaction filter"):
            call("qb_count", {"type": "customer", "status": "open"})
        self.assertEqual(call("qb_raw", {"request": "PaymentMethodQuery", "count": True}), {"count": 4})
        with self.assertRaisesRegex(ToolError, "cannot count HostQueryRq"):
            call("qb_raw", {"request": "HostQuery", "count": True})

    def test_export_writes_one_row_per_record_or_per_line_and_never_overwrites(self):
        import csv
        _, call = demo()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out", "invoices.csv")
            x = call("qb_export", {"type": "invoice", "lines": True, "csv": path})
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            every = call("qb_search", {"type": "invoice", "include_lines": True, "limit": 1000})["rows"]
            self.assertEqual((x["records"], x["rows"], len(rows)), (len(every), sum(len(r["InvoiceLineRet"]) for r in every), x["rows"]))
            first = every[-1]
            mine = [r for r in rows if r["TxnID"] == first["TxnID"]]
            self.assertEqual([r["Line.Item"] for r in mine], [line["Item"] for line in first["InvoiceLineRet"]])
            self.assertEqual((mine[0]["LineKind"], mine[0]["IsPaid"], mine[0]["Line.Quantity"]),
                             ("InvoiceLine", str(first["IsPaid"]).lower(), num_str(first["InvoiceLineRet"][0]["Quantity"])))
            with self.assertRaisesRegex(ToolError, "already exists"):
                call("qb_export", {"type": "invoice", "csv": path})
            customers = os.path.join(folder, "customers.csv")
            self.assertEqual(call("qb_export", {"type": "customer", "csv": customers})["rows"], call("qb_count", {"type": "customer"})["count"])
        with self.assertRaisesRegex(ToolError, "csv must name a .csv file"):
            call("qb_export", {"type": "invoice", "csv": "invoices.xlsx"})
        with self.assertRaisesRegex(ToolError, "transactions only"):
            call("qb_export", {"type": "customer", "lines": True, "csv": "c.csv"})

    def test_profile_shows_features_list_room_custom_fields_and_history(self):
        ctx, call = demo()
        p = call("qb_profile")
        self.assertIs(p["features"]["inventory"], True)
        self.assertIs(p["features"]["multi_currency"], False)
        entity_count = sum(call("qb_count", {"type": t, "active": "all"})["count"] for t in ("customer", "vendor", "employee", "other_name"))
        names = next(r for r in p["lists"] if r["list"] == "Entity")
        self.assertEqual((names["count"], names["max"]), (entity_count, 14500))
        self.assertEqual([f["name"] for f in p["custom_fields"]], ["Sales Region", "Bin Location"])
        self.assertIsNone(p["last_restored"])
        self.assertNotIn("warnings", p)
        # A list QuickBooks refuses to count (price levels while price rules are on) is reported, not fatal.
        request = ctx.qb.request

        def refuse_price_levels(message, body, attrs=None):
            if message == "PriceLevelQueryRq":
                raise QbStatusError(3220, "Price Level is switched off because Price Rule was switched on")
            return request(message, body, attrs)

        ctx.qb.request = refuse_price_levels
        p = call("qb_profile")
        self.assertNotIn("count", next(r for r in p["lists"] if r["list"] == "PriceLevel"))
        self.assertIn("Price Rule", p["warnings"][0])

    def test_every_record_type_can_be_counted(self):
        from qbd_agent_kit.qb.catalog import entities
        from qbd_agent_kit.qbxml.schema import supports_count
        self.assertEqual([e.key for e in entities() if not supports_count(e.query)], [])

    def test_the_audit_trail_is_never_sent_because_it_stopped_quickbooks(self):
        ctx, call = demo()
        sent: list[str] = []
        process = ctx.qb.transport.process
        ctx.qb.transport.process = lambda xml: (sent.append(xml), process(xml))[1]
        with self.assertRaisesRegex(ToolError, "qbd changes"):
            call("qb_report", {"report": "audit_trail", "from": TODAY, "to": TODAY})
        with self.assertRaisesRegex(ToolError, "unrecoverable error"):
            call("qb_raw", {"request": "GeneralDetailReportQueryRq", "body": {"GeneralDetailReportType": "AuditTrail"}})
        self.assertEqual([x for x in sent if "AuditTrail" in x], [])

    def test_changes_show_added_changed_and_deleted_records_and_edits_to_a_closed_period(self):
        ctx, call = demo()
        c = call("qb_changes", {"since": TODAY})
        self.assertEqual({t["change"] for t in c["transactions"]}, {"added"})
        self.assertEqual((c["records"], c["deleted"]), ([], []))
        self.assertNotIn("closed_period_changes", c)

        d = call("qb_draft", {"action": "add", "type": "customer", "fields": {"Name": "ZZ-TEST Changes"}})
        added = call("qb_commit", {"token": d["token"]})["ids"]["ListID"]
        old = next(r for r in ctx.qb.transport.company.txns["Invoice"] if r["TxnDate"] <= "2026-07-31")
        old["TimeModified"] = f"{TODAY}T09:00:00"  # someone edited an invoice in the closed period
        gone = next(r for r in ctx.qb.transport.company.txns["Invoice"] if r["TxnDate"] > "2026-07-31" and r is not old)
        d = call("qb_draft", {"action": "delete", "type": "invoice", "id": gone["TxnID"]})
        call("qb_commit", {"token": d["token"]})

        c = call("qb_changes", {"since": f"{TODAY}T08:00:00"})
        edited = next(t for t in c["transactions"] if t["id"] == old["TxnID"])
        self.assertEqual((edited["change"], edited["closed_period"]), ("changed", True))
        self.assertEqual(c["closed_period_changes"], 1)
        self.assertEqual([(r["list"], r["id"], r["change"]) for r in c["records"]], [("customer", added, "added")])
        self.assertEqual([(x["kind"], x["type"], x["id"]) for x in c["deleted"]], [("transaction", "Invoice", gone["TxnID"])])
        self.assertEqual(c["next_since"], f"{TODAY}T12:00:00")
        self.assertEqual(call("qb_changes", {"since": "2026-10-21"})["transactions"], [])

        many = call("qb_changes", {"since": "2026-01-01", "limit": 2})
        self.assertEqual((len(many["transactions"]), many["truncated"]), (2, True))
        self.assertNotIn("next_since", many)
        with self.assertRaisesRegex(ToolError, "since must be a date"):
            call("qb_changes", {"since": "yesterday"})

    def test_changes_warn_when_the_company_file_was_restored_since(self):
        ctx, call = demo()
        request = ctx.qb.request

        def restored(message, body, attrs=None):
            rs = request(message, body, attrs)
            if message == "CompanyActivityQueryRq":
                rs.body["CompanyActivityRet"]["LastRestoreTime"] = f"{TODAY}T07:30:00-07:00"
            return rs

        ctx.qb.request = restored
        self.assertIn("restored from a backup", call("qb_changes", {"since": TODAY})["warnings"][0])
        self.assertNotIn("warnings", call("qb_changes", {"since": f"{TODAY}T08:00:00"}))

    def test_schema_shows_the_inner_add_element_in_order(self):
        _, call = demo()
        s = call("qb_schema", {"request": "InvoiceAdd", "depth": 1})
        self.assertEqual(s["request"], "InvoiceAdd")
        self.assertLess(s["fields_in_order"].index("CustomerRef"), s["fields_in_order"].index("TxnDate"))


class Changes(unittest.TestCase):
    def test_draft_checks_names_closing_date_limits_and_lines_and_commit_is_single_use(self):
        ctx, call = demo()

        def invoice(**fields):
            return {"action": "add", "type": "invoice", "fields": {"CustomerRef": "Alder Street Cafe", **fields}}

        with self.assertRaisesRegex(ToolError, 'Did you mean: "Deck Screws 5lb"'):
            call("qb_draft", invoice(InvoiceLineAdd=[{"ItemRef": "Deck Screw", "Quantity": 1, "Rate": 1}]))
        with self.assertRaisesRegex(ToolError, "closing date"):
            call("qb_draft", invoice(TxnDate="2026-07-01", InvoiceLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 1, "Rate": 24}]))
        with self.assertRaisesRegex(ToolError, "above the limit"):
            call("qb_draft", invoice(InvoiceLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 1000, "Rate": 24}]))
        with self.assertRaisesRegex(ToolError, "not allowed here"):
            call("qb_draft", {"action": "add", "type": "journal_entry", "fields": {}})
        with self.assertRaisesRegex(QbxmlInputError, '"INV-2026-0001" has 13 characters; QuickBooks takes at most 11'):
            call("qb_draft", invoice(RefNumber="INV-2026-0001", InvoiceLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 1, "Rate": 24}]))

        d = call("qb_draft", invoice(TxnDate=TODAY, InvoiceLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 2, "Rate": 24}]))
        self.assertRegex(d["preview"], r"total 48\.00")
        self.assertEqual(len(ctx.drafts._mem), 1)
        c = call("qb_commit", {"token": d["token"]})
        self.assertIs(c["done"], True)
        self.assertTrue(c["ids"]["TxnID"])
        with self.assertRaisesRegex(ToolError, "already used"):
            call("qb_commit", {"token": d["token"]})

        with self.assertRaisesRegex(ToolError, "deletes every line left out"):
            call("qb_draft", {"action": "update", "type": "invoice", "id": c["ids"]["TxnID"], "fields": {
                "InvoiceLineMod": [{"TxnLineID": "-1", "ItemRef": "Primer 1gal", "Quantity": 1, "Rate": 24}],
            }})
        self.assertEqual(call("qb_write_log", {"limit": 1})["entries"][0]["outcome"], "done")

    def test_a_check_with_a_number_is_not_queued_for_printing(self):
        # QuickBooks marked checks added with a number "to be printed" and dropped the number.
        ctx, call = demo(QBD_WRITE_ENTITIES="check")
        sent: list[str] = []
        process = ctx.qb.transport.process
        ctx.qb.transport.process = lambda xml: (sent.append(xml), process(xml))[1]
        d = call("qb_draft", {"action": "add", "type": "check", "fields": {
            "AccountRef": "Business Checking", "PayeeEntityRef": "Riverbend Properties", "RefNumber": "5290", "TxnDate": TODAY,
            "ExpenseLineAdd": [{"AccountRef": "Rent Expense", "Amount": 100}]}})
        call("qb_commit", {"token": d["token"]})
        add = next(x for x in sent if "<CheckAddRq" in x)
        self.assertIn("<RefNumber>5290</RefNumber>", add)
        self.assertIn("<IsToBePrinted>false</IsToBePrinted>", add)

    def test_previews_show_what_quickbooks_fills_in_and_the_limit_counts_it(self):
        ctx, call = demo(QBD_WRITE_ENTITIES="sales_order,invoice,receive_payment,journal_entry,deposit,inventory_adjustment", QBD_MAX_AMOUNT="500")

        def add(type_, **fields):
            return call("qb_draft", {"action": "add", "type": type_, "fields": {"TxnDate": TODAY, **fields}})

        # Lines without a rate are priced by QuickBooks from the item list; before, they showed 0.00 and passed any limit.
        p = add("sales_order", CustomerRef="Alder Street Cafe", SalesOrderLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 6}])["preview"]
        self.assertIn("6 x Primer 1gal @ 24.00 (item price) = 144.00", p)
        self.assertIn("total about 144.00", p)
        with self.assertRaisesRegex(ToolError, r"total 600\.00 is above the limit"):
            add("sales_order", CustomerRef="Alder Street Cafe", SalesOrderLineAdd=[{"ItemRef": "Primer 1gal", "Quantity": 25}])
        with self.assertRaisesRegex(ToolError, r"total 1,187\.16 is above the limit"):
            add("invoice", CustomerRef="Elm Grove School District", LinkToTxnID=["10CD-1700000000"])

        p = add("receive_payment", CustomerRef="Granite Peak Contractors", TotalAmount=400, AppliedToTxnAdd=[{"TxnID": "109F-1700000000", "PaymentAmount": 300}])
        self.assertRegex(p["preview"], r"Applies 300\.00 to invoice 1094 \(Granite Peak Contractors, [\d-]{10}, 1,237\.00, open 618\.50\)")
        self.assertIn("Left over: 100.00 stays as a credit", p["preview"])
        self.assertNotIn("warnings", p)
        over = add("receive_payment", CustomerRef="Foxhall Landscaping", TotalAmount=50, AppliedToTxnAdd=[{"TxnID": "10B3-1700000000", "PaymentAmount": 50}])
        self.assertIn("50.00 is more than the 19.48 still open on 1105", " ".join(over["warnings"]))

        je = add("journal_entry", JournalDebitLine=[{"AccountRef": "Utilities", "Amount": 410}], JournalCreditLine=[{"AccountRef": "Accounts Payable", "Amount": 400, "EntityRef": "City Power & Light"}])
        self.assertIn("- Debit Utilities 410.00\n- Credit Accounts Payable 400.00 (City Power & Light)", je["preview"])
        self.assertIn("Debits 410.00 and credits 400.00 differ", " ".join(je["warnings"]))

        dep = add("deposit", DepositToAccountRef="Business Checking", DepositLineAdd=[{"PaymentTxnID": "1046-1700000000"}, {"PaymentTxnID": "FFFF-1700000000"}])
        self.assertIn("total 359.82", dep["preview"])
        self.assertRegex(dep["preview"], r"Deposit customer payment 5210 \(Cedar Point Marina, [\d-]{10}, 359\.82\)")
        self.assertIn("Payment FFFF-1700000000 was not found", " ".join(dep["warnings"]))

        adj = add("inventory_adjustment", AccountRef="Cost of Goods Sold", InventoryAdjustmentLineAdd=[{"ItemRef": "Primer 1gal", "QuantityAdjustment": {"NewQuantity": 29}}])
        self.assertIn("Primer 1gal: quantity 31 -> 29, value about -26.80 at average cost", adj["preview"])

    def test_a_source_is_saved_once_per_company_file_and_the_log_keeps_the_values_before(self):
        ctx, call = demo()
        source = f"email:<{os.getpid()}.{time.time()}@example.test>"  # every test in this run shares the audit log

        def bill(**more):
            return {"action": "add", "type": "bill", "source": source, **more, "fields": {
                "VendorRef": "Coastal Freight Lines", "TxnDate": TODAY, "ExpenseLineAdd": [{"AccountRef": "Freight and Delivery", "Amount": 75}]}}

        first, second = call("qb_draft", bill()), call("qb_draft", bill())
        call("qb_commit", {"token": first["token"]})
        with self.assertRaisesRegex(ToolError, "already saved"):
            call("qb_commit", {"token": second["token"]})
        with self.assertRaisesRegex(ToolError, "already saved"):
            call("qb_draft", bill())
        self.assertIs(call("qb_commit", {"token": call("qb_draft", bill(allow_duplicate=True))["token"]})["done"], True)
        self.assertEqual(committed(AUDIT, ctx.qb.host_info().company_file, source)["source"], source)
        self.assertIsNone(committed(AUDIT, "C:\\Other\\Company.qbw", source))

        # Sent, but QuickBooks never answered: it may be saved, so the source is held until someone looks.
        d = call("qb_draft", bill(source=f"{source}#2"))
        with mock.patch.object(ctx.qb, "raw", side_effect=QbConnectionError("timeout", "No answer")), self.assertRaises(WriteUnconfirmed):
            call("qb_commit", {"token": d["token"]})
        with self.assertRaisesRegex(ToolError, "never confirmed"):
            call("qb_draft", bill(source=f"{source}#2"))

        cafe = next(x for x in ctx.qb.transport.company.lists["Customer"] if x["Name"] == "Alder Street Cafe")
        phone = cafe["Phone"]
        u = call("qb_draft", {"action": "update", "type": "customer", "name": "Alder Street Cafe", "fields": {"Phone": "555-0199", "Fax": "555-0100"}})
        call("qb_commit", {"token": u["token"]})
        self.assertEqual(call("qb_write_log", {"limit": 1})["entries"][0]["before"], {"Phone": phone, "Fax": None})

    def test_undo_drafts_the_reverse_of_a_saved_change_once(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        cafe = next(x for x in c.lists["Customer"] if x["Name"] == "Alder Street Cafe")
        phone = cafe["Phone"]
        d = call("qb_draft", {"action": "update", "type": "customer", "name": "Alder Street Cafe", "fields": {"Phone": "555-0199"}})
        self.assertEqual(call("qb_commit", {"token": d["token"]})["undo"], f"qbd undo {d['token']} drafts the old values back.")
        u = call("qb_undo", {"token": d["token"]})
        self.assertIn(f"- Phone: 555-0199 -> {phone}", u["preview"])
        call("qb_commit", {"token": u["token"]})
        self.assertEqual(cafe["Phone"], phone)
        with self.assertRaisesRegex(ToolError, "already undone"):
            call("qb_undo", {"token": d["token"]})

        # An added invoice is undone by deleting it; a delete itself cannot be undone from here.
        inv = call("qb_draft", {"action": "add", "type": "invoice", "fields": {
            "CustomerRef": "Alder Street Cafe", "TxnDate": TODAY, "InvoiceLineAdd": [{"ItemRef": "Primer 1gal", "Quantity": 1, "Rate": 24}]}})
        txn_id = call("qb_commit", {"token": inv["token"]})["ids"]["TxnID"]
        gone = call("qb_undo", {"token": inv["token"]})
        self.assertRegex(gone["preview"], "^Delete invoice")
        call("qb_commit", {"token": gone["token"]})
        self.assertFalse(any(t["TxnID"] == txn_id for t in c.txns["Invoice"]))
        with self.assertRaisesRegex(ToolError, "A delete cannot be undone"):
            call("qb_undo", {"token": gone["token"]})

        # Left alone: a record changed again since, card data the log never kept, and a token never saved.
        add = call("qb_draft", {"action": "add", "type": "customer", "fields": {"Name": f"ZZ-UNDO {os.getpid()}"}})
        call("qb_commit", {"token": add["token"]})
        call("qb_commit", {"token": call("qb_draft", {"action": "update", "type": "customer", "name": f"ZZ-UNDO {os.getpid()}", "fields": {"Phone": "555-0111"}})["token"]})
        with self.assertRaisesRegex(ToolError, "changed again"):
            call("qb_undo", {"token": add["token"]})
        cafe["CreditCardInfo"] = {"CreditCardNumber": "4111111111111111", "ExpirationMonth": 12, "ExpirationYear": 2030}
        card = call("qb_draft", {"action": "update", "type": "customer", "name": "Alder Street Cafe", "fields": {"CreditCardInfo": {"CreditCardNumber": "5500000000000004"}}})
        self.assertIn('"CreditCardNumber":"***1111"', card["preview"])
        call("qb_commit", {"token": card["token"]})
        with self.assertRaisesRegex(ToolError, "CreditCardInfo cannot be put back"):
            call("qb_undo", {"token": card["token"]})
        with open(AUDIT, encoding="utf-8") as f:
            log = f.read()
        self.assertFalse(re.search("4111111111111111|5500000000000004|ExpirationYear>2030", log))
        with self.assertRaisesRegex(ToolError, "No change saved with this token"):
            call("qb_undo", {"token": "0badc0de"})

        _, off = demo(QBD_ALLOW_DELETE="false")
        add = off("qb_draft", {"action": "add", "type": "customer", "fields": {"Name": f"ZZ-UNDO-OFF {os.getpid()}"}})
        off("qb_commit", {"token": add["token"]})
        with self.assertRaisesRegex(ToolError, "turned off"):
            off("qb_undo", {"token": add["token"]})

    def test_import_drafts_a_csv_as_one_batch_and_skips_what_was_saved_before(self):
        ctx, call = demo()
        c = ctx.qb.transport.company
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, f"invoices-{os.getpid()}-{time.time_ns()}.csv")  # every test in this run shares the audit log

            def write(*rows: str) -> None:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    f.write("\n".join(["Customer,TxnDate,RefNumber,Line.Item,Line.Quantity,Line.Rate", *rows]) + "\n")

            write("Alder Street Cafe,2026-10-20,9001,Primer 1gal,2,24", "Alder Street Cafe,2026-10-20,9001,Deck Screws 5lb,1,18.5",
                  "Alder Street Cafe,2026-10-20,9002,Deck Screw,1,18.5",
                  "Alder Street Cafe,2026-10-20,9003,Primer 1gal,1,24", "Alder Street Cafe,2026-10-21,9003,Primer 1gal,1,24")
            b = call("qb_import", {"type": "invoice", "csv": path})
            self.assertEqual(b["counts"], {"records": 3, "ready": 1, "skipped": 0, "problems": 2})
            self.assertEqual([(p["rows"], p["ref"]) for p in b["problems"]], [("4", "9002"), ("5-6", "9003")])
            self.assertIn('Did you mean: "Deck Screws 5lb"', b["problems"][0]["error"])
            self.assertIn("different TxnDate", b["problems"][1]["error"])
            self.assertIn("number 9001, total 66.50", b["preview"])
            self.assertEqual(call("qb_commit", {"token": b["token"]})["counts"], {"done": 1})
            self.assertEqual(len(next(t for t in c.txns["Invoice"] if t.get("RefNumber") == "9001")["InvoiceLineRet"]), 2)

            # Fixed and imported again: the saved invoice is skipped. QuickBooks refuses one record at commit, and the rest go on.
            write("Alder Street Cafe,2026-10-20,9001,Primer 1gal,2,24", "Alder Street Cafe,2026-10-20,9001,Deck Screws 5lb,1,18.5",
                  "Alder Street Cafe,2026-10-20,9002,Deck Screws 5lb,1,18.5",
                  "Alder Street Cafe,2026-10-20,9003,Primer 1gal,1,24", "Alder Street Cafe,2026-10-20,9003,Primer 1gal,1,24")
            again = call("qb_import", {"type": "invoice", "csv": path})
            self.assertEqual(again["counts"], {"records": 3, "ready": 2, "skipped": 1, "problems": 0})
            self.assertEqual((again["skipped"][0]["ref"], again["skipped"][0]["why"][:8]), ("9001", "saved on"))
            c.lists["ItemInventory"] = [x for x in c.lists["ItemInventory"] if x["FullName"] != "Deck Screws 5lb"]
            saved = call("qb_commit", {"token": again["token"]})
            self.assertEqual((saved["done"], saved["counts"]), (False, {"done": 1, "failed": 1}))
            self.assertEqual([r["outcome"] for r in saved["results"]], ["failed", "done"])
            self.assertIn("qbd undo", saved["next_step"])
            self.assertEqual(call("qb_import", {"type": "invoice", "csv": path, "limit": 1})["counts"]["skipped"], 2)

    def test_import_lists_a_number_quickbooks_would_refuse_as_a_problem_before_anything_is_approved(self):
        _, call = demo()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, f"long-{os.getpid()}-{time.time_ns()}.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("Customer,TxnDate,RefNumber,Line.Item,Line.Quantity,Line.Rate\n"
                        "Alder Street Cafe,2026-10-20,INV-2026-0001,Primer 1gal,1,24\nAlder Street Cafe,2026-10-20,9101,Primer 1gal,1,24\n")
            b = call("qb_import", {"type": "invoice", "csv": path})
        self.assertEqual(b["counts"], {"records": 2, "ready": 1, "skipped": 0, "problems": 1})
        self.assertIn('"INV-2026-0001" has 13 characters; QuickBooks takes at most 11', b["problems"][0]["error"])

    def test_import_reads_expense_lines_journal_entries_and_list_records(self):
        ctx, call = demo(QBD_WRITE_ENTITIES="bill,journal_entry,customer")
        c = ctx.qb.transport.company
        with tempfile.TemporaryDirectory() as folder:
            def file(name: str, body: str) -> str:
                p = os.path.join(folder, f"{name}-{os.getpid()}-{time.time_ns()}.csv")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body)
                return p

            bills = call("qb_import", {"type": "bill", "csv": file("bills", "Vendor,TxnDate,RefNumber,Line.Account,Line.Amount,Line.Memo\n"
                                                                     "Coastal Freight Lines,2026-10-20,CF-9001,Freight and Delivery,120,Pallet\n"
                                                                     "Coastal Freight Lines,2026-10-20,CF-9001,Office Supplies,30,Tape\n")})
            self.assertEqual(bills["counts"]["ready"], 1)
            self.assertIn("total 150.00", bills["preview"])
            je = call("qb_import", {"type": "journal_entry", "csv": file("je", "TxnDate,RefNumber,Line.Account,Line.Debit,Line.Credit\n"
                                                                          "2026-10-20,JE-1,Rent Expense,500,\n2026-10-20,JE-1,Business Checking,,500\n")})
            self.assertEqual(je["counts"]["ready"], 1, je)
            self.assertEqual(call("qb_commit", {"token": je["token"]})["counts"], {"done": 1})
            entry = next(t for t in c.txns["JournalEntry"] if t.get("RefNumber") == "JE-1")
            self.assertEqual([k for k in entry if k.startswith("Journal")], ["JournalDebitLine", "JournalCreditLine"])

            people = call("qb_import", {"type": "customer", "csv": file("customers", "Name,Phone,BillAddress.City\n"
                                                                         "Harborview Deli,555-0170,Portland\nAlder Street Cafe,555-0171,Portland\n")})
            self.assertEqual(people["counts"], {"records": 2, "ready": 1, "skipped": 0, "problems": 1})
            self.assertEqual(people["problems"][0]["name"], "Alder Street Cafe")
            call("qb_commit", {"token": people["token"]})
            self.assertEqual(next(x for x in c.lists["Customer"] if x["Name"] == "Harborview Deli")["BillAddress"]["City"], "Portland")
            with self.assertRaisesRegex(ToolError, 'Column "Colour": there is no Colour here'):
                call("qb_import", {"type": "customer", "csv": file("bad", "Name,Colour\nX,red\n")})
            with self.assertRaisesRegex(ToolError, "not allowed here"):
                call("qb_import", {"type": "invoice", "csv": file("inv", "Customer\nX\n")})

    def test_upsert_updates_an_existing_name_and_adds_a_new_one(self):
        _, call = demo()
        u = call("qb_draft", {"action": "upsert", "type": "customer", "name": "Alder Street Cafe", "fields": {"Phone": "555-0199"}})
        self.assertRegex(u["preview"], r'^Change customer "Alder Street Cafe"')
        a = call("qb_draft", {"action": "upsert", "type": "customer", "fields": {"Name": "Mariner Supply Co"}})
        self.assertRegex(a["preview"], r'^Add customer "Mariner Supply Co"')
        with self.assertRaisesRegex(ToolError, "already exists"):
            call("qb_draft", {"action": "add", "type": "customer", "fields": {"Name": "alder street cafe"}})

    def test_delete_and_void_need_their_own_switch(self):
        _, call = demo(QBD_ALLOW_DELETE="false")
        with self.assertRaisesRegex(ToolError, "turned off"):
            call("qb_draft", {"action": "void", "type": "invoice", "id": "x"})

    def test_drafts_kept_as_files_are_single_use_and_tokens_must_look_like_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx, call = demo()
            ctx.drafts = Drafts(folder)
            d = call("qb_draft", {"action": "add", "type": "customer", "fields": {"Name": "ZZ-TEST Files"}})
            self.assertEqual(os.listdir(folder), [f"{d['token']}.json"])
            ctx.drafts = Drafts(folder)  # as a new process would
            self.assertIs(call("qb_commit", {"token": d["token"]})["done"], True)
            self.assertEqual(os.listdir(folder), [])
            for token in (d["token"], "../../x", "ABCDEF12"):
                with self.assertRaisesRegex(ToolError, "Unknown or already used token"):
                    call("qb_commit", {"token": token})


class QuickBooksPC(unittest.TestCase):
    def test_file_health_reads_the_file_its_backups_and_quickbooks_own_log(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Company Files")
            os.makedirs(os.path.join(folder, "QuickBooksAutoDataRecovery"))
            qbw = os.path.join(folder, "Sample.qbw")
            with open(qbw, "wb") as f:
                f.write(b"x" * 4096)
            with open(f"{qbw}.TLG", "wb") as f:
                f.truncate(101 * 1024 * 1024)  # sparse: bigger than the file, without writing it
            open(os.path.join(folder, "QuickBooksAutoDataRecovery", "Sample.qbw.ADR"), "wb").close()
            backups = os.path.join(root, "backups")
            os.makedirs(backups)
            backup = os.path.join(backups, "Sample (Backup Oct 10,2026).QBB")
            open(backup, "wb").close()
            os.utime(backup, (time.time() - 10 * 86400,) * 2)
            log = os.path.join(root, "AppData", "Intuit", "QuickBooks", "Log", "24.0", "QBWin.log")
            os.makedirs(os.path.dirname(log))
            with open(log, "w") as f:
                f.write("\n".join([
                    "old.c (1) :  MESSAGE: 4704: Mon Dec 29 08:00:00 LVL_ERROR--last year, outside the window",
                    "verify.c (10) :  CHECKPOINT: 4704: Mon Oct 12 08:00:00 Verify Data: QuickBooks detected no problems with your data.",
                    "trackdll.cpp (148) :  MESSAGE: 4704: Tue Oct 20 04:26:18 LVL_SEVERE_ERROR--MainFrame must be already created by now",
                    "qbw.c (2154) :  CHECKPOINT: 4704: Tue Oct 20 04:26:18 QBWMain: Entered",
                    "StorageInterface.cpp (300) :  MESSAGE: 4704: Tue Oct 20 09:56:58 LVL_FATAL_ERROR--Cannot Continue--unknown exception thrown by QuickBooks while processing an SDK request.",
                    "StorageInterface.cpp (300) :  MESSAGE: 4704: Tue Oct 20 09:57:30 LVL_FATAL_ERROR--Cannot Continue--unknown exception thrown by QuickBooks while processing an SDK request.",
                ]))
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join(root, "AppData"), "ProgramData": "", "USERPROFILE": "", "PUBLIC": ""}):
                _, call = demo()
                h = call("qb_file_health", {"file": qbw, "backup_dir": backups})
                missing = call("qb_file_health", {"file": os.path.join(root, "Gone.qbw")})
        self.assertEqual(h["file"]["tlg_mb"], 101)
        self.assertIn("auto_recovery_copy", h["file"])
        self.assertEqual(h["backups"]["newest"]["path"], backup)
        self.assertEqual((h["log"]["from"], h["log"]["to"]), ("2025-12-29T08:00:00", "2026-10-20T09:57:30"))
        self.assertEqual(h["log"]["counts"], {"fatal": 2, "severe": 1, "error": 0, "warning": 0})
        self.assertEqual((h["log"]["recent"][0]["level"], h["log"]["recent"][0]["times"]), ("fatal", 2))
        self.assertIn("no problems", h["log"]["verify"][0]["line"])
        self.assertEqual([w.split(" ")[:3] for w in h["warnings"]], [["The", "transaction", "log"], ["No", ".QBB", "backup"], ["QuickBooks", "logged", "3"]])
        self.assertIs(missing["file"]["found"], False)
        self.assertRegex(missing["warnings"][0], "^The company file is not on this computer")

    def test_web_connector_shows_each_apps_last_result_runs_and_errors_with_their_meaning(self):
        t = "\t"
        log = "\n".join([
            "", "Log file initialized at Wednesday, October 12, 2026 - 09:00 AM UTC", "Timestamp format used: YYYYMMDD.HH:MM:SS UTC",
            f"20261012.10:00:00 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : updateWS() for application = 'Old Bridge' has STARTED",
            f"20261012.10:00:05 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : Update for application = 'Old Bridge' has COMPLETED",
            f"20261014.09:00:01 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : updateWS() for application = 'Shop Sync' has STARTED",
            f"20261014.09:00:09 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : Update for application = 'Shop Sync' has COMPLETED",
            f"20261015.01:43:55 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : updateWS() for application = 'Shop Sync' has STARTED",
            f"20261015.01:43:55 UTC{t}: QBWebConnector.SOAPWebService.do_authenticate() : QBWC1012: Authentication failed due to following error message.",
            "Client found response content type of 'text/html; charset=UTF-8', but expected 'text/xml'.",
            "The request failed with the error message:", "--", "Fatal error: the shop database is down",
            f"20261015.01:43:56 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : Update completed with errors. See log (QWClog.txt) for details.",
            f"20261016.07:52:00 UTC{t}: QBWebConnector.WebServiceManager.DoUpdateSelected() : updateWS() for application = 'CRM Link' has STARTED",
            f"20261016.07:52:00 UTC{t}: QBWebConnector.SOAPWebService.CheckCFNResponse() : CRM Link: Not a valid username and password. Job ending.",
            # A scheduled run that also logs its own start, dated tomorrow in UTC; then an error that belongs to no app.
            f"20261021.01:00:00 UTC{t}: UpdateThisScheduledApp() : Performing SCHEDULED UPDATE for application = Shop Sync",
            f"20261021.01:00:01 UTC{t}: QBWebConnector.WebServiceManager.DoUpdate() : updateWS() for application = 'Shop Sync' has STARTED",
            f"20261021.01:00:09 UTC{t}: QBWebConnector.WebServiceManager.DoUpdate() : Update for application = 'Shop Sync' has COMPLETED",
            f"20261021.01:05:00 UTC{t}: QBWebConnector.LogManager.Write() : QBWC1085: There was a problem with the log file.",
        ])
        llr = "QBWC1012: Authentication failed due to following error message.\r\nClient found response content type of 'text/html; charset=UTF-8', but expected 'text/xml'."
        registry = {
            "settings": {"Level": "DEBUG"}, "version": "2.2",
            "apps": {"Shop Sync": {"AppURL": "https://shop.example.com/qbwc?key=secret", "CompanyFile": "C:\\Gone\\Shop.qbw", "UserName": "qbsync",
                                   "Password": "x" * 40, "saveThisPassword": "True", "MsgSeverity": "Error", "Slr": "Authentication failed", "Llr": llr},
                     "CRM Link": {"AppURL": "https://user:pw@crm.example.com:8443/qb", "MsgSeverity": "Information", "Slr": "Complete!", "Llr": "Done."}},
            "schedule": {"Shop Sync": {"Enabled": "true", "IntervalInSeconds": "3600"}, "CRM Link": {"Enabled": "false"}},
        }
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Intuit", "QBWebConnector", "log")
            os.makedirs(folder)
            with open(os.path.join(folder, "QWCLog.txt"), "w", encoding="utf-8") as f:
                f.write(log)
            with mock.patch.dict(os.environ, {"ProgramData": root}), \
                    mock.patch("qbd_agent_kit.tools.files.web_connector_registry", return_value=registry), \
                    mock.patch("qbd_agent_kit.tools.files.process_running", return_value=False):
                _, call = demo()
                w = call("qb_web_connector")
            with mock.patch.dict(os.environ, {"ProgramData": root}), \
                    mock.patch("qbd_agent_kit.tools.files.web_connector_registry", return_value=None), \
                    mock.patch("qbd_agent_kit.tools.files.process_running", return_value=False):
                other_user = call("qb_web_connector")
            with mock.patch.dict(os.environ, {"ProgramData": os.path.join(root, "none")}), \
                    mock.patch("qbd_agent_kit.tools.files.web_connector_registry", return_value=None):
                self.assertEqual(call("qb_web_connector")["installed"], False)
        self.assertEqual([(x["name"], x["settings_found"]) for x in other_user["apps"]], [("CRM Link", False), ("Old Bridge", False), ("Shop Sync", False)])
        self.assertIn("log only", other_user["note"])
        shop, crm, old = w["apps"]
        self.assertEqual(old, {"name": "Old Bridge", "settings_found": False, "runs_in_log": 1, "last_started": "2026-10-12T10:00:00Z",
                               "last_completed": "2026-10-12T10:00:05Z", "days_since_last_run": 8})
        self.assertNotIn("note", w)
        self.assertEqual((shop["url"], crm["url"]), ("https://shop.example.com/qbwc", "https://crm.example.com:8443/qb"), "no query string or sign-in")
        self.assertEqual((shop["auto_run"], shop["every_minutes"], shop["password_saved"], crm["auto_run"]), (True, 60, True, False))
        self.assertEqual((shop["last_result"]["status"], shop["last_result"]["code"]), ("Error", "QBWC1012"))
        self.assertIn("instead of", shop["what_it_means"])
        self.assertEqual((shop["runs_in_log"], shop["last_started"], shop["last_completed"]), (3, "2026-10-21T01:00:01Z", "2026-10-21T01:00:09Z"))
        self.assertEqual(shop["days_since_last_run"], 0)
        self.assertEqual(shop["last_error"]["detail"], "Client found response content type of 'text/html; charset=UTF-8', but expected 'text/xml'. | Fatal error: the shop database is down")
        self.assertNotIn("what_it_means", crm)
        self.assertEqual([(e["app"], e["code"], e["times"]) for e in w["log"]["errors"]],
                         [(MISSING, "QBWC1085", 1), ("CRM Link", MISSING, 1), ("Shop Sync", "QBWC1012", 1)])
        self.assertIn("password", w["log"]["errors"][1]["what_it_means"])
        self.assertEqual(w["warnings"][0], "Shop Sync: Authentication failed (QBWC1012).")
        self.assertIn("not running", w["warnings"][-1])


class Helpers(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(normalize_name("Blue Ridge Builders, Inc."), normalize_name("blue ridge builders"))
        self.assertEqual(normalize_name("Smith & Sons LLC"), "smith and sons")
        self.assertEqual(draft_total({"InvoiceLineAdd": [{"Quantity": 2, "Rate": 10.5}, {"Amount": 4}]}), 25)
        self.assertEqual(draft_total({"JournalDebitLine": [{"Amount": 100}], "JournalCreditLine": [{"Amount": 100}]}), 100)
        self.assertEqual(mask_personal_data({"SSN": "123-45-6789", "Name": "A", "EmployeePayrollInfo": {"x": 1}}), {"SSN": "***6789", "Name": "A"})
        self.assertEqual(macro_range("LastMonth", "2026-03-31"), {"from": "2026-02-01", "to": "2026-02-28"})
        self.assertEqual(macro_range("LastMonthToDate", "2026-03-31"), {"from": "2026-02-01", "to": "2026-02-28"})
        self.assertEqual(macro_range("ThisFiscalYear", "2026-03-31", 7), {"from": "2025-07-01", "to": "2026-06-30"})
        self.assertEqual(macro_range("ThisWeek", "2026-10-20"), {"from": "2026-10-18", "to": "2026-10-24"})

    def test_a_report_row_with_nothing_in_it_is_left_out(self):
        # An Audit Trail day without changes came back from QuickBooks as one row with no values.
        r = Report(None, 0, "", rows=[ReportRow("data", 0, "", [None, None]), ReportRow("data", 0, "", [None, 5.0])])
        self.assertEqual(render_report(r, 10)["rows"], [[None, 5.0]])

    def test_account_row_tries_the_full_name_first(self):
        bs = Report(None, 0, "", rows=[ReportRow("data", 0, "Operating", [100.0]), ReportRow("data", 0, "Savings", [None]),
                                       ReportRow("data", 0, "Savings:Operating", [40.0]), ReportRow("subtotal", 0, "Total Savings", [40.0])])
        self.assertEqual(account_row(bs, "Savings:Operating").values, [40.0])
        self.assertEqual(account_row(bs, "Operating").values, [100.0])
        self.assertEqual(account_row(bs, "Savings").kind, "subtotal")
        self.assertIsNone(account_row(bs, "Petty Cash"))
        # A row that shows only the last part of the name, after the account number.
        numbered = Report(None, 0, "", rows=[ReportRow("data", 0, "1020 · Operating", [40.0])])
        self.assertEqual(account_row(numbered, "Savings:Operating").values, [40.0])

    def test_missing_survives_copies(self):
        import copy

        self.assertIs(copy.deepcopy(MISSING), MISSING)


if __name__ == "__main__":
    unittest.main()
