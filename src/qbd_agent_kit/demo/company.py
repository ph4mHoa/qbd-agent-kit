"""A small made-up hardware and supply company, dated around "today" so its numbers always look current.
Every name, email (.example) and phone number (555-01xx) is fictional. It includes the problems the kit
is meant to find: overdue customers, a duplicate bill, a bill paid twice, undeposited payments, an
unapplied payment, an unapplied vendor credit, negative stock, a future-dated invoice, an old PO.

The numbers come from a seeded generator, so the company is the same on every run and every PC."""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from ..jsutil import add_up, get, round2 as money, truthy
from ..qb.dates import add_days, month_start

Rec = dict[str, Any]


class DemoCompany:
    def __init__(self, today: str):
        self.today = today
        self.name = "Bayside Hardware & Supply (sample company)"
        self.closing_date = add_days(month_start(today, -2), -1)
        self.lists: dict[str, list[Rec]] = {}   # "Customer" -> CustomerRet records
        self.txns: dict[str, list[Rec]] = {}    # "Invoice" -> InvoiceRet records
        self.undeposited: list[Rec] = []        # ReceivePaymentToDepositRet records
        self.form1099: list[Rec] = []           # Form1099CategoryAccountMappingRet records
        self.personal_data = False              # whether the app may read tax IDs and card numbers
        self.cleared: dict[str, str] = {}       # TxnID -> cleared status, where it is not the usual one
        self.home_currency: str | None = None   # multicurrency is on when set
        self.seq = 1


def _i32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def _imul(a: int, b: int) -> int:
    return _i32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def rng(seed: int) -> Callable[[], float]:
    """mulberry32: small, fast and the same everywhere."""
    state = _i32(seed)

    def next_value() -> float:
        nonlocal state
        state = _i32(state + 0x6D2B79F5)
        t = _imul(state ^ ((state & 0xFFFFFFFF) >> 15), 1 | state)
        t = _i32(t + _imul(t ^ ((t & 0xFFFFFFFF) >> 7), 61 | t)) ^ t
        return ((t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296

    return next_value


def ref(name: str, id_: str | None = None) -> Rec:
    return {"ListID": id_, "FullName": name} if id_ else {"FullName": name}


def new_list_id(c: DemoCompany) -> str:
    n = 0x80000000 + c.seq
    c.seq += 1
    return f"{n:X}-1700000000"


def new_txn_id(c: DemoCompany) -> str:
    n = 0x1000 + c.seq
    c.seq += 1
    return f"{n:X}-1700000000"


ACCOUNTS = [
    ("1010", "Business Checking", "Bank"),
    ("1020", "Business Savings", "Bank"),
    ("1100", "Accounts Receivable", "AccountsReceivable"),
    ("1300", "Inventory Asset", "OtherCurrentAsset"),
    ("1499", "Undeposited Funds", "OtherCurrentAsset"),
    ("2000", "Accounts Payable", "AccountsPayable"),
    ("2100", "Business Visa", "CreditCard"),
    ("2200", "Sales Tax Payable", "OtherCurrentLiability"),
    ("3000", "Opening Balance Equity", "Equity"),
    ("3100", "Owner's Equity", "Equity"),
    ("4000", "Merchandise Sales", "Income"),
    ("4100", "Installation Income", "Income"),
    ("4200", "Delivery Income", "Income"),
    ("5000", "Cost of Goods Sold", "CostOfGoodsSold"),
    ("6000", "Advertising and Promotion", "Expense"),
    ("6100", "Freight and Delivery", "Expense"),
    ("6200", "Insurance Expense", "Expense"),
    ("6300", "Office Supplies", "Expense"),
    ("6400", "Rent Expense", "Expense"),
    ("6500", "Utilities", "Expense"),
    ("6999", "Uncategorized Expenses", "Expense"),
]

# name, contact, terms days, pays late
CUSTOMERS = [
    ("Alder Street Cafe", "Maria Lopez", 30, False),
    ("Blue Ridge Builders", "Tom Becker", 30, False),
    ("Cedar Point Marina", "Dana Walsh", 30, False),
    ("Dunmore Property Management", "Raj Patel", 30, True),
    ("Elm Grove School District", "Accounts Payable Office", 30, False),
    ("Foxhall Landscaping", "Chris Foxhall", 15, False),
    ("Granite Peak Contractors", "Lee Morgan", 30, True),
    ("Harper & Lowe Architects", "Sam Harper", 30, False),
    ("Ironwood Fitness", "Jordan Reyes", 30, False),
    ("Juniper Dental Group", "Office Manager", 30, False),
    ("Kestrel Auto Repair", "Pat Kowalski", 15, False),
    ("Lakeside Event Center", "Alex Chen", 30, False),
]

# name, terms days, kind
VENDORS = [
    ("Summit Lumber Supply", 30, "stock"),
    ("Pioneer Paint Distributors", 30, "stock"),
    ("Metro Electric Wholesale", 30, "stock"),
    ("Harbor Hardware Wholesale", 30, "stock"),
    ("Coastal Freight Lines", 15, "Freight and Delivery"),
    ("City Power & Light", 15, "Utilities"),
    ("Oakline Insurance Agency", 30, "Insurance Expense"),
    ("Brightway Office Products", 30, "Office Supplies"),
    ("Riverbend Properties", 0, "Rent Expense"),
    ("Social Ads Co.", 0, "Advertising and Promotion"),
]

# name, cost, price, vendor, reorder point, max, on hand
STOCK = [
    ("Deck Screws 5lb", 18.5, 32.99, "Harbor Hardware Wholesale", 40, 150, 22),
    ("Cedar Board 2x6x8", 14.2, 24.5, "Summit Lumber Supply", 100, 400, 260),
    ("Pressure-Treated Post 4x4x8", 11.75, 19.99, "Summit Lumber Supply", 60, 240, 48),
    ("Exterior Paint 1gal White", 21, 38, "Pioneer Paint Distributors", 30, 120, 75),
    ("Exterior Paint 5gal White", 89, 149, "Pioneer Paint Distributors", 8, 30, 5),
    ("Primer 1gal", 13.4, 24, "Pioneer Paint Distributors", 24, 96, 31),
    ("LED Flood Light 50W", 27.6, 54.99, "Metro Electric Wholesale", 15, 60, -3),
    ("Outdoor Outlet Cover", 4.1, 9.49, "Metro Electric Wholesale", 40, 200, 140),
    ("Wire 12/2 250ft", 82, 129, "Metro Electric Wholesale", 6, 24, 9),
    ("Wood Stain 1gal Cedar", 19.8, 36.5, "Pioneer Paint Distributors", 20, 80, 12),
    ("Hinge Set Heavy Duty", 6.3, 12.99, "Harbor Hardware Wholesale", 30, 120, 64),
    ("Concrete Mix 80lb", 5.4, 8.99, "Summit Lumber Supply", 80, 300, 95),
]


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().replace("&", "and"))
    s = s[1:] if s.startswith("-") else s
    return s[:-1] if s.endswith("-") else s


def create_demo_company(today: str) -> DemoCompany:
    r = rng(20261020)

    def pick(xs: list) -> Any:
        return xs[math.floor(r() * len(xs))]

    def between(a: int, b: int) -> int:
        return a + math.floor(r() * (b - a + 1))

    c = DemoCompany(today)
    counters = {"invoice": 1040, "check": 5210, "bill": 0}

    def next_number(key: str) -> int:
        n = counters[key]
        counters[key] += 1
        return n

    def time(date: str) -> str:
        return f"{date}T10:15:00"

    def lst(base: str, rec: Rec) -> Rec:
        full = {"ListID": new_list_id(c), "TimeCreated": time(add_days(today, -400)), "TimeModified": time(add_days(today, -30)), "EditSequence": "1700000000", **rec}
        c.lists.setdefault(base, []).append(full)
        return full

    def txn(base: str, rec: Rec) -> Rec:
        date = min(rec["TxnDate"], today)  # a transaction dated ahead was still entered by today
        full = {"TxnID": new_txn_id(c), "TimeCreated": time(date), "TimeModified": time(date), "EditSequence": "1700000000", "TxnNumber": c.seq, **rec}
        c.txns.setdefault(base, []).append(full)
        return full

    for num, name, type_ in ACCOUNTS:
        lst("Account", {"Name": name, "FullName": name, "IsActive": True, "Sublevel": 0, "AccountType": type_, "AccountNumber": num, "Balance": 0, "TotalBalance": 0,
                        **({"SpecialAccountType": "SalesTaxPayable"} if name == "Sales Tax Payable" else {})})
    for name, days in (("Net 30", 30), ("Net 15", 15), ("Due on receipt", 0)):
        lst("StandardTerms", {"Name": name, "IsActive": True, "StdDueDays": days})
    for name in ("Check", "ACH", "Visa", "Cash"):
        lst("PaymentMethod", {"Name": name, "IsActive": True})

    customers = [
        lst("Customer", {
            "Name": name, "FullName": name, "IsActive": True, "Sublevel": 0, "CompanyName": name, "Contact": contact,
            "Phone": f"555-01{between(10, 99)}", "Email": f"billing@{_slug(name)}.example",
            "TermsRef": ref("Net 15" if terms == 15 else "Net 30"), "Balance": 0, "TotalBalance": 0,
        })
        for name, contact, terms, _late in CUSTOMERS
    ]
    # A near-duplicate entered by someone who did not search first.
    lst("Customer", {
        "Name": "Blue Ridge Builders Inc.", "FullName": "Blue Ridge Builders Inc.", "IsActive": True, "Sublevel": 0,
        "CompanyName": "Blue Ridge Builders Inc.", "Email": customers[1]["Email"], "Balance": 0, "TotalBalance": 0,
    })
    cash_customer = lst("Customer", {"Name": "Cash Sales", "FullName": "Cash Sales", "IsActive": True, "Sublevel": 0, "Balance": 0, "TotalBalance": 0})

    for name, terms, kind in VENDORS:
        lst("Vendor", {
            "Name": name, "IsActive": True, "CompanyName": name, "Phone": f"555-01{between(10, 99)}",
            "Email": f"ar@{_slug(name)}.example", "TermsRef": ref("Net 15" if terms == 15 else "Due on receipt" if terms == 0 else "Net 30"), "Balance": 0,
            **({} if kind == "stock" else {"PrefillAccountRef": [ref(kind)]}),
            **({"IsVendorEligibleFor1099": True, "VendorAddress": {"Addr1": "88 Riverbend Road", "City": "Riverbend", "State": "CA"}}
               if name == "Riverbend Properties" else {}),
        })
    # The rent account is mapped to 1099-MISC box 1, with the threshold QuickBooks shipped before 2026.
    c.form1099 = [{"FormType": "1099-MISC", "IsFiling1099Misc": True,
                   "CategoryAccountMapping": [{"MappingCategory": "Box 1: Rents", "AccountRef": [ref("Rent Expense")], "Threshold": 600}]}]

    for name, cost, price, vendor, rp, max_, on_hand in STOCK:
        lst("ItemInventory", {
            "Name": name, "FullName": name, "IsActive": True, "Sublevel": 0, "SalesDesc": name, "SalesPrice": price,
            "IncomeAccountRef": ref("Merchandise Sales"), "PurchaseDesc": name, "PurchaseCost": cost, "COGSAccountRef": ref("Cost of Goods Sold"),
            "PrefVendorRef": ref(vendor), "AssetAccountRef": ref("Inventory Asset"), "ReorderPoint": rp, "Max": max_,
            "QuantityOnHand": on_hand, "AverageCost": cost, "QuantityOnOrder": 0, "QuantityOnSalesOrder": 0,
        })
    lst("ItemService", {
        "Name": "Installation Labor", "FullName": "Installation Labor", "IsActive": True, "Sublevel": 0,
        "SalesOrPurchase": {"Desc": "Installation labor, per hour", "Price": 85, "AccountRef": ref("Installation Income")},
    })
    lst("ItemOtherCharge", {
        "Name": "Delivery Fee", "FullName": "Delivery Fee", "IsActive": True, "Sublevel": 0,
        "SalesOrPurchase": {"Desc": "Local delivery", "Price": 45, "AccountRef": ref("Delivery Income")},
    })
    stock = c.lists["ItemInventory"]

    def sales_lines(prefix: str, n: int) -> list[Rec]:
        lines: list[Rec] = []
        for i in range(n):
            item = pick(stock)
            qty = between(2, 24)
            lines.append({"TxnLineID": f"{prefix}-{i + 1}", "ItemRef": ref(item["FullName"], item["ListID"]), "Desc": item["SalesDesc"], "Quantity": qty, "Rate": item["SalesPrice"], "Amount": money(qty * item["SalesPrice"])})
        if r() < 0.25:
            hours = between(2, 8)
            lines.append({"TxnLineID": f"{prefix}-L", "ItemRef": ref("Installation Labor"), "Desc": "Installation labor, per hour", "Quantity": hours, "Rate": 85, "Amount": hours * 85})
        if r() < 0.2:
            lines.append({"TxnLineID": f"{prefix}-D", "ItemRef": ref("Delivery Fee"), "Desc": "Local delivery", "Quantity": 1, "Rate": 45, "Amount": 45})
        return lines

    def total(lines: list[Rec]) -> float:
        return money(add_up(line["Amount"] for line in lines))

    def payment(customer: Rec, date: str, amount: float, applied: list[Rec], unused: float = 0) -> Rec:
        return txn("ReceivePayment", {
            "TxnDate": date, "RefNumber": str(next_number("check")), "CustomerRef": ref(customer["FullName"], customer["ListID"]),
            "ARAccountRef": ref("Accounts Receivable"), "TotalAmount": money(amount), "PaymentMethodRef": ref(pick(["Check", "ACH"])),
            "DepositToAccountRef": ref("Undeposited Funds"), "UnusedPayment": money(unused), "UnusedCredits": 0, "AppliedToTxnRet": applied,
        })

    # Invoices over the last five months, and the payments that settled them.
    for i in range(70):
        name, _contact, terms, late = CUSTOMERS[i % len(CUSTOMERS)]
        customer = next(x for x in customers if x["Name"] == name)
        date = add_days(today, -between(1, 150))
        due = add_days(date, terms)
        lines = sales_lines(f"I{i}", between(1, 3))
        sub = total(lines)
        overdue = due < today
        chance = 0.85 if date < add_days(today, -60) else 0.5 if overdue else 0.25
        if late:
            chance *= 0.35
        paid = r() < chance
        partial = not paid and overdue and r() < 0.12
        inv = txn("Invoice", {
            "CustomerRef": ref(name, customer["ListID"]), "ARAccountRef": ref("Accounts Receivable"), "TxnDate": date,
            "RefNumber": str(next_number("invoice")), "TermsRef": ref("Net 15" if terms == 15 else "Net 30"), "DueDate": due,
            "Subtotal": sub, "SalesTaxPercentage": 0, "SalesTaxTotal": 0, "AppliedAmount": 0, "BalanceRemaining": sub, "IsPaid": False,
            "InvoiceLineRet": lines,
        })
        if paid or partial:
            amount = sub if paid else money(sub / 2)
            pay_date = min(add_days(date, between(5, terms + 12)), add_days(today, -1))
            payment(customer, pay_date, amount, [{"TxnID": inv["TxnID"], "TxnType": "Invoice", "TxnDate": date, "RefNumber": inv["RefNumber"], "Amount": amount}])
            inv["AppliedAmount"] = -amount
            inv["BalanceRemaining"] = money(sub - amount)
            inv["IsPaid"] = paid
    # An invoice typed with the wrong month: dated 45 days in the future.
    customer = customers[11]
    lines = sales_lines("F", 2)
    txn("Invoice", {
        "CustomerRef": ref(customer["FullName"], customer["ListID"]), "ARAccountRef": ref("Accounts Receivable"),
        "TxnDate": add_days(today, 45), "RefNumber": str(next_number("invoice")), "TermsRef": ref("Net 30"), "DueDate": add_days(today, 75),
        "Subtotal": total(lines), "SalesTaxPercentage": 0, "SalesTaxTotal": 0, "AppliedAmount": 0, "BalanceRemaining": total(lines), "IsPaid": False, "InvoiceLineRet": lines,
    })
    # A customer payment nobody applied to an invoice.
    payment(customers[9], add_days(today, -20), 500, [], 500)

    # Cash sales.
    for i in range(8):
        lines = sales_lines(f"S{i}", between(1, 2))
        txn("SalesReceipt", {
            "CustomerRef": ref("Cash Sales", cash_customer["ListID"]), "TxnDate": add_days(today, -between(1, 60)), "RefNumber": f"CS-{300 + i}",
            "PaymentMethodRef": ref(pick(["Cash", "Visa"])), "DepositToAccountRef": ref("Undeposited Funds"),
            "Subtotal": total(lines), "SalesTaxTotal": 0, "TotalAmount": total(lines), "SalesReceiptLineRet": lines,
        })

    # Payments and cash sales not yet deposited: the last three days, plus two forgotten ones.
    payments = c.txns.get("ReceivePayment", [])
    forgotten = {p["TxnID"] for p in [x for x in payments if x["TxnDate"] < add_days(today, -9)][-2:]}
    for t in payments + c.txns.get("SalesReceipt", []):
        recent = t["TxnDate"] >= add_days(today, -3)
        if not recent and t["TxnID"] not in forgotten:
            continue
        c.undeposited.append({
            "TxnID": t["TxnID"], "TxnType": "SalesReceipt" if "SalesReceiptLineRet" in t else "ReceivePayment",
            "CustomerRef": t["CustomerRef"], "TxnDate": t["TxnDate"], "RefNumber": t["RefNumber"], "Amount": t["TotalAmount"],
        })

    # Estimates and sales orders; one estimate and one sales order have sat open for months.
    for i in range(6):
        customer = customers[(i * 5) % len(customers)]
        lines = sales_lines(f"E{i}", 2)
        txn("Estimate", {
            "CustomerRef": ref(customer["FullName"], customer["ListID"]), "TxnDate": add_days(today, -(130 if i == 0 else between(5, 60))),
            "RefNumber": f"E-{210 + i}", "DueDate": add_days(today, 30), "Subtotal": total(lines), "TotalAmount": total(lines), "IsActive": i % 2 == 0, "EstimateLineRet": lines,
        })
    for i in range(4):
        customer = customers[(i * 3 + 1) % len(customers)]
        lines = sales_lines(f"O{i}", 2)
        for line in lines:
            item = next((s for s in stock if s["FullName"] == line["ItemRef"]["FullName"]), None)
            if item:
                item["QuantityOnSalesOrder"] = item["QuantityOnSalesOrder"] + line["Quantity"]
        txn("SalesOrder", {
            "CustomerRef": ref(customer["FullName"], customer["ListID"]), "TxnDate": add_days(today, -(100 if i == 0 else between(2, 20))),
            "RefNumber": f"SO-{880 + i}", "DueDate": add_days(today, 10), "Subtotal": total(lines), "TotalAmount": total(lines),
            "IsManuallyClosed": False, "IsFullyInvoiced": False, "SalesOrderLineRet": lines,
        })

    # Purchase orders: two still open, one of them three months old.
    def po(vendor: str, date: str, item: str, qty: int, open_: bool, ref_number: str) -> None:
        it = next(s for s in stock if s["FullName"] == item)
        if open_:
            it["QuantityOnOrder"] = it["QuantityOnOrder"] + qty
        txn("PurchaseOrder", {
            "VendorRef": ref(vendor), "TxnDate": date, "RefNumber": ref_number, "DueDate": add_days(date, 14),
            "TotalAmount": money(qty * it["PurchaseCost"]), "IsManuallyClosed": False, "IsFullyReceived": not open_,
            "PurchaseOrderLineRet": [{"TxnLineID": f"{ref_number}-1", "ItemRef": ref(item, it["ListID"]), "Desc": item, "Quantity": qty, "Rate": it["PurchaseCost"], "Amount": money(qty * it["PurchaseCost"]), "ReceivedQuantity": 0 if open_ else qty}],
        })

    po("Pioneer Paint Distributors", add_days(today, -8), "Wood Stain 1gal Cedar", 40, True, "PO-1501")
    po("Metro Electric Wholesale", add_days(today, -97), "Outdoor Outlet Cover", 60, True, "PO-1468")
    po("Summit Lumber Supply", add_days(today, -40), "Cedar Board 2x6x8", 120, False, "PO-1482")
    po("Harbor Hardware Wholesale", add_days(today, -25), "Hinge Set Heavy Duty", 60, False, "PO-1490")

    # Bills: stock purchases and running costs.
    def bill(vendor: str, date: str, ref_number: str, lines: list[Rec], terms: int) -> Rec:
        amount = total(lines)
        return txn("Bill", {
            "VendorRef": ref(vendor), "APAccountRef": ref("Accounts Payable"), "TxnDate": date, "DueDate": add_days(date, terms),
            "AmountDue": amount, "RefNumber": ref_number, "TermsRef": ref("Net 15" if terms == 15 else "Net 30"), "IsPaid": False, "OpenAmount": amount,
            **({"ItemLineRet": lines} if truthy(lines[0].get("ItemRef")) else {"ExpenseLineRet": lines}),
        })

    def expense(account: str, amount: float, id_: str) -> list[Rec]:
        return [{"TxnLineID": id_, "AccountRef": ref(account), "Amount": money(amount)}]

    for i in range(14):
        vendor = VENDORS[i % 4][0]
        items = [s for s in stock if s["PrefVendorRef"]["FullName"] == vendor]
        lines = []
        for k in (0, 1):
            it = pick(items)
            qty = between(10, 60)
            lines.append({"TxnLineID": f"B{i}-{k}", "ItemRef": ref(it["FullName"], it["ListID"]), "Desc": it["PurchaseDesc"], "Quantity": qty, "Cost": it["PurchaseCost"], "Amount": money(qty * it["PurchaseCost"])})
        bill(vendor, add_days(today, -between(3, 100)), f"INV-{7000 + next_number('bill')}", lines, 30)
    for m in range(1, 5):
        bill("City Power & Light", add_days(month_start(today, -m + 1), 4), f"CPL-{2600 + m}", expense("Utilities", 380 + between(0, 140), f"U{m}"), 15)
    bill("Oakline Insurance Agency", add_days(today, -70), "OIA-3317", expense("Insurance Expense", 1850, "IN1"), 30)
    for i in range(5):
        bill("Coastal Freight Lines", add_days(today, -between(15, 90)), f"CF-{1150 + i}", expense("Freight and Delivery", 140 + between(0, 460), f"F{i}"), 15)
    # The same freight invoice entered twice, and the same office order billed twice.
    bill("Coastal Freight Lines", add_days(today, -12), "CF-1182", expense("Freight and Delivery", 486.2, "FD1"), 15)
    bill("Coastal Freight Lines", add_days(today, -10), "CF-1182", expense("Freight and Delivery", 486.2, "FD2"), 15)
    bill("Brightway Office Products", add_days(today, -33), "BW-5531", expense("Office Supplies", 129.95, "OS1"), 30)
    bill("Brightway Office Products", add_days(today, -31), "BW-5538", expense("Office Supplies", 129.95, "OS2"), 30)

    # Pay most bills that came due more than five days ago, but leave some overdue.
    for bl in c.txns["Bill"]:
        if bl["DueDate"] >= add_days(today, -5) or r() < 0.15 or bl["RefNumber"].startswith("CF-118"):
            continue
        amount = bl["AmountDue"]
        txn("BillPaymentCheck", {
            "PayeeEntityRef": bl["VendorRef"], "APAccountRef": ref("Accounts Payable"), "TxnDate": min(add_days(bl["DueDate"], -2), add_days(today, -1)),
            "BankAccountRef": ref("Business Checking"), "Amount": amount, "RefNumber": str(next_number("check")),
            "AppliedToTxnRet": [{"TxnID": bl["TxnID"], "TxnType": "Bill", "TxnDate": bl["TxnDate"], "RefNumber": bl["RefNumber"], "Amount": amount}],
        })
        bl["IsPaid"] = True
        bl["OpenAmount"] = 0

    # Checks: rent each month, an expense nobody categorised, and a utility bill paid by check
    # while the bill itself stays open.
    def check(payee: str, date: str, account: str, amount: float, memo: str | None = None) -> Rec:
        rec: Rec = {"AccountRef": ref("Business Checking"), "PayeeEntityRef": ref(payee), "RefNumber": str(next_number("check")), "TxnDate": date, "Amount": money(amount)}
        if memo:
            rec["Memo"] = memo
        rec["ExpenseLineRet"] = expense(account, amount, f"C{counters['check']}")
        return txn("Check", rec)

    for m in range(5):
        check("Riverbend Properties", month_start(today, -m), "Rent Expense", 3200, "Monthly rent")
    check("Brightway Office Products", add_days(month_start(today, -1), 9), "Uncategorized Expenses", 312.4)
    open_utility = next((x for x in c.txns["Bill"] if x["VendorRef"]["FullName"] == "City Power & Light" and not x["IsPaid"]), None)
    if open_utility:
        check("City Power & Light", add_days(open_utility["TxnDate"], 3), "Utilities", open_utility["AmountDue"])

    for i in range(6):
        date = add_days(today, -between(1, 120))
        if i % 2:
            payee, account, amount = "Brightway Office Products", "Office Supplies", 60 + between(0, 180)
        else:
            payee, account, amount = "Social Ads Co.", "Advertising and Promotion", 200 + between(0, 150)
        txn("CreditCardCharge", {"AccountRef": ref("Business Visa"), "PayeeEntityRef": ref(payee), "TxnDate": date, "Amount": amount, "ExpenseLineRet": expense(account, amount, f"CC{i}")})

    # A vendor credit for a damaged delivery, not applied to any bill yet.
    txn("VendorCredit", {
        "VendorRef": ref("Coastal Freight Lines"), "APAccountRef": ref("Accounts Payable"), "TxnDate": add_days(today, -6), "RefNumber": "CF-CR-21",
        "CreditAmount": 95, "OpenAmount": 95, "Memo": "Damaged delivery refund", "ExpenseLineRet": expense("Freight and Delivery", 95, "VC1"),
    })

    recompute_balances(c)
    # Bank and card items clear a week after their date, except a check never cashed and a bill payment marked in
    # a reconciliation someone left unfinished.
    c.cleared[next(x for x in c.txns["Check"] if x.get("Memo") is None and x["PayeeEntityRef"]["FullName"] == "Brightway Office Products")["TxnID"]] = "NotCleared"
    c.cleared[max((x for x in c.txns["BillPaymentCheck"] if x["TxnDate"] <= add_days(today, -14)), key=lambda x: x["TxnDate"])["TxnID"]] = "Pending"
    # Two inventory sites (Advanced Inventory), with fixed IDs so records added later keep theirs.
    for i, (name, desc) in enumerate((("Main Store", "Shop floor and front counter"), ("Warehouse", "Back warehouse")), 1):
        c.lists.setdefault("InventorySite", []).append({
            "ListID": f"8000F00{i}-1700000000", "TimeCreated": time(add_days(today, -400)), "TimeModified": time(add_days(today, -400)),
            "EditSequence": "1700000000", "Name": name, "IsActive": True, "IsDefaultSite": i == 1, "SiteDesc": desc,
        })
    return c


def _or0(v: Any) -> Any:
    return v if truthy(v) else 0


def recompute_balances(c: DemoCompany) -> None:
    """Customer, vendor and account balances follow from the open transactions."""

    def total(xs: list[Rec] | None, f: str) -> float:
        return money(add_up(_or0(x.get(f)) for x in xs or []))

    for cu in c.lists.get("Customer", []):
        def own(x: Rec) -> bool:
            return isinstance(x.get("CustomerRef"), dict) and x["CustomerRef"].get("FullName") == cu.get("FullName")

        invoices = [x for x in c.txns["Invoice"] if own(x)] if "Invoice" in c.txns else None
        payments = [x for x in c.txns["ReceivePayment"] if own(x)] if "ReceivePayment" in c.txns else None
        open_ = total(invoices, "BalanceRemaining") - total(payments, "UnusedPayment")
        cu["Balance"] = money(open_)
        cu["TotalBalance"] = money(open_)
    for v in c.lists.get("Vendor", []):
        def mine(x: Rec) -> bool:
            return get(x["VendorRef"], "FullName") == v.get("Name")

        bills = [x for x in c.txns["Bill"] if mine(x)] if "Bill" in c.txns else None
        credits = [x for x in c.txns["VendorCredit"] if mine(x)] if "VendorCredit" in c.txns else None
        v["Balance"] = money(total(bills, "OpenAmount") - total(credits, "OpenAmount"))

    def set_(name: str, value: float) -> None:
        a = next((x for x in c.lists["Account"] if x["Name"] == name), None)
        if a:
            a["Balance"] = a["TotalBalance"] = money(value)

    set_("Business Checking", 48250.17)
    set_("Business Savings", 120000)
    set_("Business Visa", 3410.55)
    # Unapplied payments and credits lower the A/R and A/P balances, as in QuickBooks.
    set_("Accounts Receivable", total(c.lists.get("Customer"), "Balance"))
    set_("Accounts Payable", total(c.lists.get("Vendor"), "Balance"))
    set_("Undeposited Funds", total(c.undeposited, "Amount"))
    set_("Inventory Asset", add_up(max(0, i["QuantityOnHand"]) * i["AverageCost"] for i in c.lists.get("ItemInventory", [])))
