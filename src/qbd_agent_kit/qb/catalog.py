"""The records an agent can search, under plain names. Everything else (which filters a query takes,
which fields a record has) is read from the schema, so the catalog stays a list of names."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..errors import ToolError
from ..jsutil import MISSING, is_num, nz
from ..qbxml.schema import message_model, model


@dataclass(frozen=True)
class Entity:
    key: str            # "invoice"
    base: str           # qbXML name, "Invoice"
    kind: str           # "list" or "txn"
    label: str
    query: str          # "InvoiceQueryRq"
    fields: tuple       # every field the record can have, in schema order


LISTS = [
    ("customer", "Customer", "customers and jobs"),
    ("vendor", "Vendor", "vendors"),
    ("employee", "Employee", "employees (no payroll data)"),
    ("other_name", "OtherName", "other names"),
    ("item", "Item", "all items of every type"),
    ("item_inventory", "ItemInventory", "inventory parts"),
    ("item_assembly", "ItemInventoryAssembly", "inventory assemblies"),
    ("item_non_inventory", "ItemNonInventory", "non-inventory parts"),
    ("item_service", "ItemService", "service items"),
    ("item_other_charge", "ItemOtherCharge", "other charges"),
    ("item_group", "ItemGroup", "item groups"),
    ("item_discount", "ItemDiscount", "discount items"),
    ("account", "Account", "chart of accounts with balances"),
    ("class", "Class", "classes"),
    ("terms", "Terms", "payment terms"),
    ("payment_method", "PaymentMethod", "payment methods"),
    ("sales_rep", "SalesRep", "sales reps"),
    ("sales_tax_code", "SalesTaxCode", "sales tax codes"),
    ("price_level", "PriceLevel", "price levels"),
    ("inventory_site", "InventorySite", "inventory sites (Enterprise)"),
    ("ship_method", "ShipMethod", "shipping methods"),
    ("customer_type", "CustomerType", "customer types"),
    ("vendor_type", "VendorType", "vendor types"),
    ("currency", "Currency", "currencies"),
]

TXNS = [
    ("invoice", "Invoice", "invoices"),
    ("estimate", "Estimate", "estimates / quotes"),
    ("sales_order", "SalesOrder", "sales orders"),
    ("sales_receipt", "SalesReceipt", "sales receipts"),
    ("credit_memo", "CreditMemo", "credit memos"),
    ("receive_payment", "ReceivePayment", "customer payments"),
    ("deposit", "Deposit", "bank deposits"),
    ("charge", "Charge", "statement charges"),
    ("bill", "Bill", "vendor bills"),
    ("vendor_credit", "VendorCredit", "vendor credits"),
    ("bill_payment_check", "BillPaymentCheck", "bill payments by check"),
    ("bill_payment_credit_card", "BillPaymentCreditCard", "bill payments by credit card"),
    ("check", "Check", "checks / expenses"),
    ("credit_card_charge", "CreditCardCharge", "credit card charges"),
    ("credit_card_credit", "CreditCardCredit", "credit card credits"),
    ("purchase_order", "PurchaseOrder", "purchase orders"),
    ("item_receipt", "ItemReceipt", "item receipts (received inventory)"),
    ("journal_entry", "JournalEntry", "journal entries"),
    ("inventory_adjustment", "InventoryAdjustment", "inventory adjustments"),
    ("build_assembly", "BuildAssembly", "assembly builds"),
    ("transfer", "Transfer", "transfers between accounts"),
    ("transfer_inventory", "TransferInventory", "inventory transfers between sites"),
    ("sales_tax_payment", "SalesTaxPaymentCheck", "sales tax payments"),
    ("time_tracking", "TimeTracking", "time entries"),
    ("transaction", "Transaction", "any transaction type (summary only)"),
]


def _make(kind: str, key: str, base: str, label: str) -> Entity | None:
    query = f"{base}QueryRq"
    rs = message_model(f"{base}QueryRs")
    if message_model(query) is None or rs is None:
        return None
    fields: list[str] = []
    for name in rs.o:
        if not name.endswith("Ret"):
            continue
        m = model(rs.c[name][0])
        for f in m.o if m else []:
            if f not in fields:
                fields.append(f)
    return Entity(key, base, kind, label, query, tuple(fields))


@lru_cache(maxsize=None)
def entities() -> tuple[Entity, ...]:
    made = [_make("list", *e) for e in LISTS] + [_make("txn", *e) for e in TXNS]
    return tuple(e for e in made if e is not None)


def entity(key: str) -> Entity:
    for e in entities():
        if e.key == key:
            return e
    raise ToolError(f'Unknown record type "{key}". Use one of: {", ".join(x.key for x in entities())}')


# Fields shown in search results, in this order when the record has them.
SUMMARY = [
    "_type", "ListID", "TxnID", "TxnType", "FullName", "Name", "CompanyName", "AccountType", "AccountNumber",
    "RefNumber", "TxnDate", "DueDate", "CustomerRef", "VendorRef", "PayeeEntityRef", "EntityRef",
    "AccountRef", "DepositToAccountRef", "BankAccountRef", "CreditCardAccountRef", "Email", "Phone", "Contact",
    "Balance", "TotalBalance", "Subtotal", "SalesTaxTotal", "TotalAmount", "Amount", "AmountDue", "DepositTotal",
    "OpenAmount", "BalanceRemaining", "AppliedAmount", "UnusedPayment", "UnusedCredits", "CreditRemaining",
    "IsPaid", "IsPending", "IsManuallyClosed", "IsFullyInvoiced", "IsFullyReceived", "IsActive",
    "QuantityOnHand", "QuantityOnOrder", "QuantityOnSalesOrder", "ReorderPoint", "Max", "SalesPrice",
    "PurchaseCost", "AverageCost", "PrefVendorRef", "SalesOrPurchase", "SalesAndPurchase", "Memo",
]


def summary_fields(e: Entity) -> list[str]:
    has = set(e.fields)
    # QuickBooks can fill a bill's OpenAmount with the vendor's whole open balance, which reads like the bill's own.
    return [f for f in SUMMARY if f in has and not (f == "Name" and "FullName" in has) and not (f == "OpenAmount" and e.base == "Bill")]


def party_field(e: Entity) -> str | None:
    """The party a transaction belongs to (customer, vendor, payee), as a field name."""
    return next((f for f in ("CustomerRef", "VendorRef", "PayeeEntityRef", "EntityRef") if f in e.fields), None)


def amount_of(row: dict) -> Any:
    """A transaction's total, whichever field holds it for this type; MISSING when it has none."""
    for f in ("TotalAmount", "AmountDue", "Amount", "DepositTotal"):
        if is_num(row.get(f)):
            return row[f]
    if is_num(row.get("Subtotal")):
        tax = row.get("SalesTaxTotal")
        return row["Subtotal"] + (tax if is_num(tax) and tax == tax else 0)
    return MISSING


def ref_value(ref: dict) -> Any:
    return nz(ref.get("FullName", MISSING), ref.get("ListID", MISSING), ref.get("TxnID", MISSING))


def compact(row: dict, fields: list[str] | None = None) -> dict:
    """{ListID, FullName} references read as their name; everything else is kept."""
    out = {}
    for k, v in row.items():
        if fields is not None and k not in fields:
            continue
        if isinstance(v, dict) and k.endswith("Ref"):
            out[k[:-3]] = ref_value(v)
        else:
            out[k] = v
    return out


def ref_name(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return nz(v.get("FullName", MISSING), v.get("ListID", MISSING)) if isinstance(v, dict) else MISSING
    return v if isinstance(v, str) else MISSING
