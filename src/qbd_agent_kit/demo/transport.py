"""Answers qbXML like QuickBooks would, from the sample company in memory, so every command can be
tried (and tested) without QuickBooks. It covers the queries, reports and writes the tools use."""
from __future__ import annotations

import math
import re
from typing import Any

from ..jsutil import MISSING, add_up, get, is_num, js_round, num_str, nz, round2 as money, text, to_fixed, to_num, truthy
from ..qb.dates import add_days, days_between, macro_range
from ..qbxml import xml
from ..qbxml.build import escape_xml
from .company import Rec, create_demo_company, new_list_id, new_txn_id, recompute_balances

RAW = "#raw"  # pre-built XML, for report rows whose order matters
ITEM_TYPES = ["ItemService", "ItemNonInventory", "ItemOtherCharge", "ItemInventory", "ItemInventoryAssembly", "ItemGroup", "ItemDiscount"]
LIST_BASES = [
    "Customer", "Vendor", "Employee", "OtherName", "Account", "Class", "PaymentMethod", "SalesRep", "SalesTaxCode", "PriceLevel",
    "ShipMethod", "CustomerType", "VendorType", "Currency", "InventorySite", "StandardTerms", *ITEM_TYPES,
]
BUCKETS = ["Current", "1 - 30", "31 - 60", "61 - 90", "> 90"]
COMPANY_FILE = "C:\\Users\\Public\\Documents\\Intuit\\QuickBooks\\Company Files\\Bayside Hardware (sample).qbw"


class Status(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _arr(v: Any) -> list:
    if v is None or v is MISSING:
        return []
    return v if isinstance(v, list) else [v]


def _str(v: Any) -> Any:
    return MISSING if v is None or v is MISSING else text(v)


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _end(n: float) -> int:
    """A list slice end from any number, as a count: NaN is 0, fractions are cut."""
    return 0 if n != n else int(n) if math.isfinite(n) else (1 << 62 if n > 0 else 0)


def to_xml(name: str, value: Any) -> str:
    if isinstance(value, list):
        return "".join(to_xml(name, v) for v in value)
    if value is None or value is MISSING:
        return ""
    if isinstance(value, dict):
        attrs = []
        body = []
        for k, v in value.items():
            if k.startswith("@"):
                attrs.append(f' {k[1:]}="{escape_xml(text(v))}"')
            elif k == RAW:
                body.append(text(v))
            else:
                body.append(to_xml(k, v))
        return f"<{name}{''.join(attrs)}>{''.join(body)}</{name}>"
    t = num_str(js_round(value * 100000) / 100000) if is_num(value) else text(value)
    return f"<{name}>{escape_xml(t)}</{name}>"


def amount_of(r: Rec) -> Any:
    for f in ("TotalAmount", "AmountDue", "Amount", "DepositTotal", "CreditAmount"):
        if is_num(r.get(f)):
            return r[f]
    return r["Subtotal"] if is_num(r.get("Subtotal")) else MISSING


def _text_match(name: str, needle: str, how: str) -> bool:
    a, b = name.lower(), needle.lower()
    return a.startswith(b) if how == "StartsWith" else a.endswith(b) if how == "EndsWith" else b in a


def _bucket(days_late: float) -> int:
    return 0 if days_late <= 0 else 1 if days_late <= 30 else 2 if days_late <= 60 else 3 if days_late <= 90 else 4


def _js_min(*xs: float) -> float:
    return math.nan if any(x != x for x in xs) else min(xs)


def _report_rows(rows: list[dict]) -> dict:
    """ReportData mixes row types in order, which the dict form cannot express, so build it as XML."""
    out = []
    for n, r in enumerate(rows, 1):
        if r["kind"] == "text":
            out.append(f'<TextRow rowNumber="{n}" value="{escape_xml(r["label"])}"/>')
            continue
        tag = "DataRow" if r["kind"] == "data" else "SubtotalRow" if r["kind"] == "subtotal" else "TotalRow"
        row_data = f'<RowData rowType="{r["rowType"]}" value="{escape_xml(r["label"])}"/>' if r.get("rowType") else ""
        cols = [f'<ColData colID="1" value="{escape_xml(r["label"])}"/>']
        cols += [f'<ColData colID="{i + 2}" value="{escape_xml(to_fixed(v, 2) if is_num(v) else v)}"/>' for i, v in enumerate(r.get("values") or [])]
        out.append(f'<{tag} rowNumber="{n}">{row_data}{"".join(cols)}</{tag}>')
    return {RAW: "".join(out)}


class DemoTransport:
    kind = "demo"

    def __init__(self, today: str):
        self.company = create_demo_company(today)
        self._iterators: dict[str, dict] = {}
        self._next_iterator = 1
        self.deleted: dict[str, list[Rec]] = {"TxnDeletedRet": [], "ListDeletedRet": []}

    def open(self) -> dict[str, Any]:
        return {"companyFile": COMPANY_FILE}

    def close(self) -> None:
        pass

    def process(self, qbxml: str) -> str:
        doc = xml.parse(qbxml)
        rq = get(doc.get("QBXML"), "QBXMLMsgsRq")
        out = []
        for message, value in (rq if isinstance(rq, dict) else {}).items():
            if message.startswith("@"):
                continue
            for item in _arr(value):
                out.append(self._answer(message, _dict(item)))
        return f'<?xml version="1.0" ?>\n<QBXML><QBXMLMsgsRs>{"".join(out)}</QBXMLMsgsRs></QBXML>'

    def _answer(self, message: str, rq: Rec) -> str:
        rs_name = message[:-2] + "Rs" if message.endswith("Rq") else message
        attrs = {"@requestID": rq.get("@requestID", MISSING)}
        try:
            body, extra = self._handle(message, rq)
            empty = all(isinstance(v, list) and not v for v in body.values())
            if empty and message.endswith("QueryRq") and "Report" not in message:
                ok = {"@statusCode": "1", "@statusSeverity": "Info", "@statusMessage": "A query request did not find a matching object in QuickBooks"}
            else:
                ok = {"@statusCode": "0", "@statusSeverity": "Info", "@statusMessage": "Status OK"}
            return to_xml(rs_name, {**attrs, **ok, **(extra or {}), **body})
        except Exception as e:
            code = e.code if isinstance(e, Status) else 3000
            return to_xml(rs_name, {**attrs, "@statusCode": str(code), "@statusSeverity": "Error", "@statusMessage": str(e)})

    def _handle(self, message: str, rq: Rec) -> tuple[dict, dict | None]:
        c = self.company
        if message == "HostQueryRq":
            ret = {"ProductName": "QuickBooks Desktop sample company (qbd-agent-kit demo)", "MajorVersion": "34", "MinorVersion": "0", "Country": "US", "SupportedQBXMLVersion": ["13.0", "14.0", "15.0", "16.0"], "IsAutomaticLogin": "false", "QBFileMode": "MultiUser"}
            if truthy(get(rq, "IncludeListMetaData")):
                # Made-up limits for the sample company, in the shape QuickBooks reports them.
                ret["ListMetaData"] = {f"{k}MetaData": {"MaxCapacity": str(n)} for k, n in (("Account", 10000), ("Class", 10000), ("Entity", 14500), ("Item", 14500), ("PaymentMethod", 10000), ("PriceLevel", 100), ("SalesRep", 10000), ("SalesTaxCode", 10000), ("Terms", 10000))}
            return {"HostRet": ret}, None
        if message == "CompanyActivityQueryRq":
            never = "1969-12-31T16:00:00-08:00"
            return {"CompanyActivityRet": {"LastRestoreTime": never, "LastCondenseTime": never}}, None
        if message in ("TxnDeletedQueryRq", "ListDeletedQueryRq"):
            ret = message.replace("QueryRq", "Ret")
            key = "TxnDelType" if ret == "TxnDeletedRet" else "ListDelType"
            types = [text(x) for x in _arr(rq.get(key))]
            since = text(nz(get(get(rq, "DeletedDateRangeFilter"), "FromDeletedDate"), ""))
            return {ret: [r for r in self.deleted[ret] if r[key] in types and text(r["TimeDeleted"]) >= since]}, None
        if message == "DataExtDefQueryRq":
            return {"DataExtDefRet": [
                {"OwnerID": "0", "DataExtID": "1", "DataExtName": "Sales Region", "DataExtType": "STR255TYPE", "AssignToObject": ["Customer"]},
                {"OwnerID": "0", "DataExtID": "2", "DataExtName": "Bin Location", "DataExtType": "STR255TYPE", "AssignToObject": ["Item"]},
            ]}, None
        if message == "CompanyQueryRq":
            return {"CompanyRet": {"IsSampleCompany": "true", "CompanyName": c.name, "LegalCompanyName": c.name, "Phone": "555-0100", "Email": "office@bayside-hardware.example", "FirstMonthFiscalYear": "January", "FirstMonthIncomeTaxYear": "January", "TaxForm": "Form1120S"}}, None
        if message == "PreferencesQueryRq":
            return {"PreferencesRet": {"AccountingPreferences": {"IsUsingAccountNumbers": "true", "IsRequiringAccounts": "true", "IsUsingClassTracking": "false", "IsUsingAuditTrail": "true", "IsAssigningJournalEntryNumbers": "true", "ClosingDate": c.closing_date}, "JobsAndEstimatesPreferences": {"IsUsingEstimates": "true", "IsUsingProgressInvoicing": "false", "IsPrintingItemsWithZeroAmounts": "false"}, "MultiCurrencyPreferences": {"IsMultiCurrencyOn": "true", "HomeCurrencyRef": {"FullName": c.home_currency}} if c.home_currency else {"IsMultiCurrencyOn": "false"}, "MultiLocationInventoryPreferences": {"IsMultiLocationInventoryAvailable": "true", "IsMultiLocationInventoryEnabled": "true"}, "PurchasesAndVendorsPreferences": {"IsUsingInventory": "true", "DaysBillsAreDue": "10", "IsAutomaticallyUsingDiscounts": "false"}, "CurrentAppAccessRights": {"IsAutomaticLoginAllowed": "false", "IsPersonalDataAccessAllowed": "true" if c.personal_data else "false"}}}, None
        if message == "Form1099CategoryAccountMappingQueryRq":
            return {"Form1099CategoryAccountMappingRet": c.form1099}, None
        if message == "ReceivePaymentToDepositQueryRq":
            return {"ReceivePaymentToDepositRet": c.undeposited}, None
        if message == "ItemQueryRq":
            return self._query(rq, [(f"{t}Ret", r) for t in ITEM_TYPES for r in c.lists.get(t, [])], "list")
        if message == "ItemSitesQueryRq":
            return self._query(rq, [("ItemSitesRet", r) for r in self._item_sites(rq)], "list")
        if message == "TermsQueryRq":
            return self._query(rq, [("StandardTermsRet", r) for r in c.lists.get("StandardTerms", [])], "list")
        if message == "TransactionQueryRq":
            return self._query(rq, self._all_transactions(), "transaction")
        if message in ("GeneralSummaryReportQueryRq", "GeneralDetailReportQueryRq", "AgingReportQueryRq"):
            return {"ReportRet": self._report(message, rq)}, None
        if message in ("TxnDelRq", "TxnVoidRq"):
            return self._delete_txn(message, rq), None
        if message == "ListDelRq":
            return self._delete_list(rq), None
        base = re.sub(r"(Query|Add|Mod)Rq\Z", "", message)
        if message.endswith("QueryRq"):
            if base in c.lists or base in LIST_BASES:
                return self._query(rq, [(f"{base}Ret", r) for r in c.lists.get(base, [])], "list")
            return self._query(rq, [(f"{base}Ret", r) for r in c.txns.get(base, [])], "txn")
        if message.endswith("AddRq"):
            return {f"{base}Ret": self._add(base, _dict(nz(rq.get(f"{base}Add", MISSING), {})))}, None
        if message.endswith("ModRq"):
            return {f"{base}Ret": self._mod(base, _dict(nz(rq.get(f"{base}Mod", MISSING), {})))}, None
        raise Status(3250, f"The sample company does not support {message}.")

    # ---------------------------------------------------------------- queries

    def _query(self, rq: Rec, all_rows: list[tuple[str, Rec]], kind: str) -> tuple[dict, dict | None]:
        iterator = _str(rq.get("@iterator", MISSING))
        if iterator in ("Continue", "Stop"):
            id_ = nz(_str(rq.get("@iteratorID", MISSING)), "")
            it = self._iterators.get(id_)
            if not it:
                raise Status(3170, f"The iterator {id_} is not valid or has expired.")
            if iterator == "Stop":
                del self._iterators[id_]
                return {}, {"@iteratorRemainingCount": "0", "@iteratorID": id_}
            size = _end(to_num(nz(rq.get("MaxReturned", MISSING), 100)))
            page = it["rows"][it["pos"]:it["pos"] + size]
            it["pos"] += len(page)
            return self._group(page, rq), {"@iteratorRemainingCount": str(len(it["rows"]) - it["pos"]), "@iteratorID": id_}
        rows = [x for x in all_rows if self._matches(x[1], rq, kind)]
        if kind != "list":
            rows.sort(key=lambda x: text(get(x[1], "TxnDate")))
        size = _end(to_num(rq["MaxReturned"])) if "MaxReturned" in rq else len(rows)
        meta = _str(rq.get("@metaData", MISSING))
        if meta == "MetaDataOnly":
            return {}, {"@retCount": str(len(rows))}
        if meta == "MetaDataAndResponseData":
            # QuickBooks Enterprise 24 counts what it returns here, not every match.
            return self._group(rows[:size], rq), {"@retCount": str(len(rows[:size]))}
        if iterator == "Start":
            id_ = f"{{demo-{self._next_iterator}}}"
            self._next_iterator += 1
            page = rows[:size]
            self._iterators[id_] = {"rows": rows, "pos": len(page)}
            return self._group(page, rq), {"@iteratorRemainingCount": str(len(rows) - len(page)), "@iteratorID": id_}
        return self._group(rows[:size], rq), None

    def _group(self, rows: list[tuple[str, Rec]], rq: Rec) -> dict:
        include = [text(x) for x in _arr(rq.get("IncludeRetElement"))]
        lines = rq.get("IncludeLineItems") == "true"
        linked = rq.get("IncludeLinkedTxns") == "true" and (not include or "LinkedTxn" in include)
        body: dict[str, list] = {}
        for ret, r in rows:
            rec = r
            if include:
                rec = {k: v for k, v in r.items() if k in include}
            elif not lines:
                rec = {k: v for k, v in r.items() if not re.search(r"Line(Group)?Ret\Z", k)}
            # Like QuickBooks, an open bill reports its vendor's whole open balance as its OpenAmount.
            if ret == "BillRet" and "OpenAmount" in rec and r.get("IsPaid") is False:
                vendor = next((v for v in self.company.lists.get("Vendor", []) if v.get("Name") == get(get(r, "VendorRef"), "FullName")), None)
                rec = {**rec, "OpenAmount": nz(vendor.get("Balance", MISSING) if vendor else MISSING, r.get("OpenAmount", MISSING))}
            if linked and "TxnID" in r:
                rec = {**rec, **({"LinkedTxn": links} if (links := self._linked(r)) else {})}
            body.setdefault(ret, []).append(rec)
        return body

    def _linked(self, r: Rec) -> list[Rec]:
        """LinkedTxn as QuickBooks reports it: payments and credits applied to this transaction (AMTTYPE), and the orders
        it was made from or into with LinkToTxnID (QUANTYPE)."""
        me, out = r.get("TxnID"), []
        for base, rows in self.company.txns.items():
            for t in rows:
                applied = [x for x in _arr(t.get("AppliedToTxnRet")) if get(x, "TxnID") == me]
                made = me in [text(x) for x in _arr(t.get("LinkToTxnID"))] or t.get("TxnID") in [text(x) for x in _arr(r.get("LinkToTxnID"))]
                if applied or made:
                    out.append({"TxnID": t["TxnID"], "TxnType": base, "TxnDate": t.get("TxnDate"), "RefNumber": t.get("RefNumber"),
                                "LinkType": "AMTTYPE" if applied else "QUANTYPE",
                                "Amount": -add_up(to_num(get(x, "Amount")) for x in applied) if applied else amount_of(t)})
        return out

    def _matches(self, r: Rec, rq: Rec, kind: str) -> bool:
        name = nz(get(r, "FullName"), get(r, "Name"))
        ids = [text(x) for x in _arr(rq.get("ListID"))]
        if ids:
            return r.get("ListID") in ids
        names = [text(x) for x in _arr(rq.get("FullName"))]
        if names:
            return isinstance(name, str) and any(n.lower() == name.lower() for n in names)
        txn_ids = [text(x) for x in _arr(rq.get("TxnID"))]
        if txn_ids:
            return r.get("TxnID") in txn_ids
        refs = [text(x) for x in _arr(rq.get("RefNumber"))]
        if refs:
            return r.get("RefNumber") in refs
        currencies = [text(x).lower() for x in _arr(get(get(rq, "CurrencyFilter"), "FullName"))]
        if currencies and text(get(get(r, "CurrencyRef"), "FullName")).lower() not in currencies:
            return False

        if kind == "list":
            active = nz(_str(rq.get("ActiveStatus", MISSING)), "ActiveOnly")
            if active == "ActiveOnly" and r.get("IsActive") is False:
                return False
            if active == "InactiveOnly" and r.get("IsActive") is not False:
                return False
            nf = rq.get("NameFilter", MISSING)
            if truthy(nf) and not _text_match(nz(name, ""), text(nz(get(nf, "Name"), "")), text(nz(get(nf, "MatchCriterion"), "Contains"))):
                return False
            if truthy(rq.get("AccountType")) and r.get("AccountType") != rq["AccountType"]:
                return False
            if truthy(rq.get("FromModifiedDate")) and text(get(r, "TimeModified")) < text(rq["FromModifiedDate"]):
                return False
            return True
        rng = self._range(nz(get(rq, "TxnDateRangeFilter"), get(rq, "TransactionDateRangeFilter")))
        if rng and (text(get(r, "TxnDate")) < rng["from"] or text(get(r, "TxnDate")) > rng["to"]):
            return False
        mod = nz(get(rq, "ModifiedDateRangeFilter"), get(rq, "TransactionModifiedDateRangeFilter"))
        if truthy(get(mod, "FromModifiedDate")) and text(get(r, "TimeModified")) < text(get(mod, "FromModifiedDate")):
            return False
        ef = nz(get(rq, "EntityFilter"), get(rq, "TransactionEntityFilter"))
        if truthy(ef):
            party = nz(get(r, "CustomerRef"), get(r, "VendorRef"), get(r, "PayeeEntityRef"), get(r, "EntityRef"))
            wanted = [text(x).lower() for k in ("FullName", "FullNameWithChildren", "ListID", "ListIDWithChildren") for x in _arr(get(ef, k))]
            own = [text(x).lower() for x in (get(party, "FullName"), get(party, "ListID")) if truthy(x)]
            if not any(o == w or o.startswith(f"{w}:") for w in wanted for o in own):
                return False
        paid = _str(get(rq, "PaidStatus"))
        if paid == "NotPaidOnly" and r.get("IsPaid") is not False:
            return False
        if paid == "PaidOnly" and r.get("IsPaid") is not True:
            return False
        types = [text(x) for x in _arr(get(get(rq, "TransactionTypeFilter"), "TxnTypeFilter"))]
        if types and "All" not in types and text(get(r, "TxnType")) not in types:
            return False
        return True

    def _item_sites(self, rq: Rec) -> list[Rec]:
        """Stock per site: the warehouse holds 40% of what is on hand and receives the purchase orders, and sales
        orders ship from the store."""
        f = get(rq, "ItemSiteFilter")
        items = {text(x).lower() for x in _arr(get(get(f, "ItemFilter"), "FullName"))}
        sites = {text(x).lower() for x in _arr(get(get(f, "SiteFilter"), "FullName"))}
        store, warehouse = self.company.lists["InventorySite"]
        out = []
        for item in self.company.lists.get("ItemInventory", []):
            if items and text(item["FullName"]).lower() not in items:
                continue
            back = math.floor(max(0, item["QuantityOnHand"]) * 0.4)
            split = ((store, item["QuantityOnHand"] - back, item["QuantityOnSalesOrder"], 0), (warehouse, back, 0, item["QuantityOnOrder"]))
            for i, (site, on_hand, on_so, on_po) in enumerate(split, 1):
                if sites and site["Name"].lower() not in sites:
                    continue
                out.append({
                    "ListID": f"{item['ListID']}-S{i}", "ItemInventoryRef": {"ListID": item["ListID"], "FullName": item["FullName"]},
                    "InventorySiteRef": {"ListID": site["ListID"], "FullName": site["Name"]},
                    **({"ReorderLevel": item["ReorderPoint"]} if site is store and item.get("ReorderPoint") else {}),
                    "QuantityOnHand": on_hand, "QuantityOnPurchaseOrders": on_po, "QuantityOnSalesOrders": on_so,
                    "QuantityToBeBuiltByPendingBuildTxns": 0, "QuantityRequiredByPendingBuildTxns": 0, "QuantityOnPendingTransfers": 0,
                })
        return out

    def _range(self, f: Any) -> dict[str, str] | None:
        if not truthy(f):
            return None
        if truthy(get(f, "DateMacro")):
            return macro_range(text(f["DateMacro"]), self.company.today)
        return {"from": nz(_str(get(f, "FromTxnDate")), "1900-01-01"), "to": nz(_str(get(f, "ToTxnDate")), "2999-12-31")}

    def _all_transactions(self) -> list[tuple[str, Rec]]:
        out = []
        for base, rows in self.company.txns.items():
            for r in rows:
                # Like QuickBooks, a bill's amount comes back negative here.
                amount = -amount_of(r) if base == "Bill" and truthy(amount_of(r)) else amount_of(r)
                out.append(("TransactionRet", {
                    "TxnType": base, "TxnID": get(r, "TxnID"), "TimeCreated": get(r, "TimeCreated"), "TimeModified": get(r, "TimeModified"), "TxnDate": get(r, "TxnDate"),
                    "RefNumber": get(r, "RefNumber"), "EntityRef": nz(get(r, "CustomerRef"), get(r, "VendorRef"), get(r, "PayeeEntityRef")),
                    "Amount": amount, "Memo": get(r, "Memo"),
                    # In a multicurrency file the amount is in the transaction's currency, and its home amount at its rate.
                    **({"CurrencyRef": r["CurrencyRef"], "ExchangeRate": r["ExchangeRate"], "AmountInHomeCurrency": money(amount * r["ExchangeRate"])}
                       if "ExchangeRate" in r else {}),
                }))
        return out

    # ---------------------------------------------------------------- reports

    def _report(self, message: str, rq: Rec) -> dict:
        c = self.company
        type_ = text(nz(get(rq, "GeneralSummaryReportType"), get(rq, "GeneralDetailReportType"), get(rq, "AgingReportType")))
        period = get(rq, "ReportPeriod")
        if truthy(get(rq, "ReportDateMacro")):
            rng = macro_range(text(rq["ReportDateMacro"]), c.today) or {"from": c.today, "to": c.today}
        else:
            rng = {"from": nz(_str(get(period, "FromReportDate")), add_days(c.today, -30)), "to": nz(_str(get(period, "ToReportDate")), c.today)}
        basis = nz(_str(get(rq, "ReportBasis")), "Accrual")
        if message == "AgingReportQueryRq" and type_ in ("ARAgingSummary", "APAgingSummary"):
            return self._aging(type_, rng["to"])
        if message == "AgingReportQueryRq" and type_ in ("ARAgingDetail", "APAgingDetail"):
            f = get(rq, "ReportEntityFilter")
            return self._aging_detail(type_, rng["to"], _str(nz(get(f, "FullNameWithChildren"), get(f, "FullName"))))
        if type_ == "ProfitAndLossStandard":
            return self._profit_and_loss(rng, basis)
        if type_ == "SalesByItemSummary":
            return self._sales_by_item(rng)
        if type_ == "SalesByCustomerSummary":
            return self._sales_by_customer(rng)
        if type_ in ("OpenSalesOrderByCustomer", "OpenPOs"):
            return self._open_orders(type_, rng)
        if type_ == "TxnDetailByAccount":
            return self._register(rq, rng)
        if type_ == "SalesTaxLiability":
            return self._sales_tax_liability(rng)
        if type_ == "BalanceSheetStandard":
            return self._balance_sheet(rng, basis)
        if type_ == "InventoryValuationDetail":
            return self._valuation_detail(rq, rng)
        raise Status(3250, f"The sample company has no {type_} report. It has ProfitAndLossStandard, SalesByItemSummary, SalesByCustomerSummary, ARAgingSummary, ARAgingDetail, APAgingSummary, APAgingDetail, OpenSalesOrderByCustomer, OpenPOs, TxnDetailByAccount, InventoryValuationDetail, SalesTaxLiability and BalanceSheetStandard.")

    def _open_orders(self, type_: str, rng: dict[str, str]) -> dict:
        """Sales orders or purchase orders still open, one row each, grouped by customer or vendor."""
        so = type_ == "OpenSalesOrderByCustomer"
        src = self.company.txns.get("SalesOrder" if so else "PurchaseOrder", [])
        open_ = [
            t for t in src
            if not truthy(t.get("IsManuallyClosed")) and not truthy(t.get("IsFullyInvoiced" if so else "IsFullyReceived"))
            and rng["from"] <= text(get(t, "TxnDate")) <= rng["to"]
        ]

        def party(t: Rec) -> str:
            return text(get(nz(get(t, "CustomerRef"), get(t, "VendorRef")), "FullName"))

        rows: list[dict] = []
        for name in sorted(dict.fromkeys(party(t) for t in open_)):
            own = [t for t in open_ if party(t) == name]
            rows.append({"kind": "text", "label": name})
            for t in own:
                rows.append({"kind": "data", "label": "", "values": ["Sales Order" if so else "Purchase Order", text(get(t, "TxnDate")), text(get(t, "RefNumber")), name, t["TotalAmount"], text(get(t, "TxnID"))]})
            rows.append({"kind": "subtotal", "label": f"Total {name}", "values": ["", "", "", "", money(add_up(t["TotalAmount"] for t in own)), ""]})
        rows.append({"kind": "total", "label": "TOTAL", "values": ["", "", "", "", money(add_up(t["TotalAmount"] for t in open_)), ""]})
        cols = [("", "Blank", "STRTYPE"), ("Type", "TxnType", "STRTYPE"), ("Date", "Date", "DATETYPE"), ("Num", "RefNumber", "STRTYPE"), ("Name", "Name", "STRTYPE"), ("Amount", "Amount", "AMTTYPE"), ("", "TxnID", "STRTYPE")]
        return self._report_xml("Open Sales Orders by Customer" if so else "Open Purchase Orders", rng, None, cols, rows)

    def _register(self, rq: Rec, rng: dict[str, str]) -> dict:
        """Transaction Detail by Account: payments out of bank and card accounts, and expense and journal lines posted to
        any account (a check written to Sales Tax Payable, say). One text row per account, then its lines."""
        c = self.company
        f = get(rq, "ReportAccountFilter")
        kind, names = _str(get(f, "AccountTypeFilter")), [text(x) for x in _arr(get(f, "FullName"))]
        accounts = [a["Name"] for a in c.lists["Account"] if (a["Name"] in names if names else not kind or a.get("AccountType") == kind)]
        types = {text(x) for x in _arr(get(get(rq, "ReportTxnTypeFilter"), "TxnTypeFilter"))}
        shown = {"Check": "Check", "BillPaymentCheck": "Bill Pmt -Check", "CreditCardCharge": "Credit Card Charge"}
        lines = []
        for base, label in shown.items():
            for t in c.txns.get(base, []) if not types or base in types else []:
                account = text(get(nz(get(t, "BankAccountRef"), get(t, "AccountRef")), "FullName"))
                if account in accounts and rng["from"] <= t["TxnDate"] <= rng["to"]:
                    status = c.cleared.get(t["TxnID"], "Cleared" if t["TxnDate"] <= add_days(c.today, -7) else "NotCleared")
                    lines.append({"TxnID": t["TxnID"], "TxnType": label, "Date": t["TxnDate"], "RefNumber": text(nz(get(t, "RefNumber"), "")),
                                  "Name": text(get(get(t, "PayeeEntityRef"), "FullName")), "Memo": text(nz(get(t, "Memo"), "")), "Account": account,
                                  "ClearedStatus": "" if status == "NotCleared" else status, "Amount": -t["Amount"],
                                  "ModifiedTime": text(t["TimeModified"]), "LastModifiedBy": "Admin"})
        posted = {"Check": "Check", "Bill": "Bill", "CreditCardCharge": "Credit Card Charge", "JournalEntry": "General Journal"}
        for base, label in posted.items():
            for t in c.txns.get(base, []) if not types or base in types else []:
                for key, sign in (("ExpenseLineRet", 1), ("JournalDebitLine", 1), ("JournalCreditLine", -1)):
                    for line in _arr(get(t, key)):
                        account = text(get(get(line, "AccountRef"), "FullName"))
                        if account in accounts and rng["from"] <= t["TxnDate"] <= rng["to"]:
                            party = nz(get(line, "EntityRef"), get(t, "PayeeEntityRef"), get(t, "VendorRef"))
                            lines.append({"TxnID": t["TxnID"], "TxnType": label, "Date": t["TxnDate"], "RefNumber": text(nz(get(t, "RefNumber"), "")),
                                          "Name": text(nz(get(party, "FullName"), "")), "Memo": text(nz(get(line, "Memo"), get(t, "Memo"), "")),
                                          "Account": account, "ClearedStatus": "", "Amount": sign * line["Amount"],
                                          "ModifiedTime": text(t["TimeModified"]), "LastModifiedBy": "Admin"})
        titles = {"TxnID": "", "TxnType": "Type", "Date": "Date", "RefNumber": "Num", "Name": "Name", "Memo": "Memo", "Account": "Account",
                  "ClearedStatus": "Clr", "Amount": "Amount", "ModifiedTime": "Last modified", "LastModifiedBy": "Last modified by"}
        wanted = [text(x) for x in _arr(get(rq, "IncludeColumn"))] or ["TxnType", "Date", "RefNumber", "Name", "Memo", "ClearedStatus", "Amount"]
        cols = [k for k in titles if k in wanted]
        rows: list[dict] = []
        for account in accounts:
            own = sorted((x for x in lines if x["Account"] == account), key=lambda x: x["Date"])
            if not own:
                continue
            rows.append({"kind": "text", "label": account})
            rows += [{"kind": "data", "label": "", "values": [x[k] for k in cols]} for x in own]
            rows.append({"kind": "subtotal", "label": f"Total {account}", "values": [money(add_up(x["Amount"] for x in own)) if k == "Amount" else "" for k in cols]})
        rows.append({"kind": "total", "label": "TOTAL", "values": [money(add_up(x["Amount"] for x in lines)) if k == "Amount" else "" for k in cols]})
        types = {"Date": "DATETYPE", "Amount": "AMTTYPE"}
        return self._report_xml("Transaction Detail By Account", rng, None, [("", "Blank", "STRTYPE"), *((titles[k], k, types.get(k, "STRTYPE")) for k in cols)], rows)

    def _valuation_detail(self, rq: Rec, rng: dict[str, str]) -> dict:
        """Inventory Valuation Detail: each item's movements with the quantity on hand after each. The sample company
        keeps only the current quantity on an item, so earlier ones are worked back from it, transactions dated ahead
        included, as QuickBooks counts them in an item's quantity; costs stay at the item's average cost."""
        c = self.company
        names = {text(x).lower() for x in _arr(get(get(rq, "ReportItemFilter"), "FullName"))}
        kinds = {"Invoice": ("Invoice", -1), "SalesReceipt": ("Sales Receipt", -1), "CreditMemo": ("Credit Memo", 1), "Bill": ("Bill", 1),
                 "ItemReceipt": ("Item Receipt", 1), "Check": ("Check", 1), "CreditCardCharge": ("Credit Card Charge", 1)}
        titles = {"TxnID": "", "TxnType": "Type", "Date": "Date", "Name": "Name", "RefNumber": "Num", "Quantity": "Qty",
                  "QuantityOnHand": "On Hand", "AverageCost": "Avg Cost", "ValueOnHand": "Asset Value"}
        wanted = [text(x) for x in _arr(get(rq, "IncludeColumn"))] or [k for k in titles if k != "TxnID"]
        cols = [k for k in titles if k in wanted]
        rows: list[dict] = []
        for item in c.lists.get("ItemInventory", []):
            if names and item["FullName"].lower() not in names:
                continue
            moves = []
            for base, (label, sign) in kinds.items():
                for t in c.txns.get(base, []):
                    for k, lines in t.items():
                        if k.endswith("LineRet") and k != "ExpenseLineRet":
                            moves += [(t, label, sign * line["Quantity"]) for line in _arr(lines) if get(get(line, "ItemRef"), "FullName") == item["FullName"]]
            moves.sort(key=lambda m: (m[0]["TxnDate"], m[0]["TxnNumber"]))
            on_hand = item["QuantityOnHand"] - add_up(qty for _, _, qty in moves)
            shown = []
            for t, label, qty in moves:
                on_hand += qty
                if rng["from"] <= t["TxnDate"] <= rng["to"]:
                    party = text(get(nz(get(t, "CustomerRef"), get(t, "VendorRef"), get(t, "PayeeEntityRef")), "FullName"))
                    shown.append({"TxnID": t["TxnID"], "TxnType": label, "Date": t["TxnDate"], "Name": party, "RefNumber": text(nz(get(t, "RefNumber"), "")),
                                  "Quantity": qty, "QuantityOnHand": on_hand, "AverageCost": item["AverageCost"], "ValueOnHand": money(on_hand * item["AverageCost"])})
            if not shown:
                continue
            rows.append({"kind": "text", "label": item["FullName"]})
            rows += [{"kind": "data", "label": "", "values": [x[k] for k in cols]} for x in shown]
            end = shown[-1]
            rows.append({"kind": "subtotal", "label": f"Total {item['FullName']}", "values": [end[k] if k in ("QuantityOnHand", "ValueOnHand") else "" for k in cols]})
        types = {"Date": "DATETYPE", "Quantity": "QUANTYPE", "QuantityOnHand": "QUANTYPE", "AverageCost": "PRICETYPE", "ValueOnHand": "AMTTYPE"}
        return self._report_xml("Inventory Valuation Detail", rng, None, [("", "Blank", "STRTYPE"), *((titles[k], k, types.get(k, "STRTYPE")) for k in cols)], rows)

    def _sales_tax_liability(self, rng: dict[str, str]) -> dict:
        """Sales Tax Liability, its TOTAL row only: the sample company charges no sales tax unless a test gives a sale a
        SalesTaxTotal, and has no tax agencies to group by."""
        c = self.company

        def tax(base: str) -> float:
            return add_up(to_num(get(t, "SalesTaxTotal")) for t in c.txns.get(base, []) if t["TxnDate"] <= rng["to"])

        paid = add_up(t["Amount"] for t in c.txns.get("SalesTaxPaymentCheck", []) if t["TxnDate"] <= rng["to"])
        owed = money(tax("Invoice") + tax("SalesReceipt") - tax("CreditMemo") - paid)
        cols = [("", "Blank", "STRTYPE"), (["Sales Tax Payable", f"As of {rng['to']}"], "Amount", "AMTTYPE")]
        return self._report_xml("Sales Tax Liability", rng, None, cols, [{"kind": "total", "label": "TOTAL", "values": [owed]}])

    def _balance_sheet(self, rng: dict[str, str], basis: str) -> dict:
        """Balance Sheet: today's account balances whatever the date or basis, without totals: the sample company keeps no
        history of its balances and no equity close-out."""
        groups = {"ASSETS": ("Bank", "AccountsReceivable", "OtherCurrentAsset", "FixedAsset", "OtherAsset"),
                  "LIABILITIES": ("AccountsPayable", "CreditCard", "OtherCurrentLiability", "LongTermLiability"), "EQUITY": ("Equity",)}
        rows: list[dict] = []
        for heading, kinds in groups.items():
            rows.append({"kind": "text", "label": heading})
            rows += [{"kind": "data", "label": a["Name"], "rowType": "account", "values": [money(a["Balance"])]}
                     for a in self.company.lists["Account"] if a.get("AccountType") in kinds]
        return self._report_xml("Balance Sheet", {"from": rng["to"], "to": rng["to"]}, basis, [("", "Blank", "STRTYPE"), (rng["to"], "Amount", "AMTTYPE")], rows)

    def _report_xml(self, title: str, rng: dict[str, str], basis: str | None, columns: list[tuple], rows: list[dict]) -> dict:
        """Column titles may have several rows, top first, as QuickBooks sends them."""

        def titles(t: Any) -> list[str]:
            return t if isinstance(t, list) else [t]

        out: dict[str, Any] = {"ReportTitle": title, "ReportSubtitle": rng["to"] if rng["from"] == rng["to"] else f"{rng['from']} through {rng['to']}"}
        if basis:
            out["ReportBasis"] = basis
        out["NumRows"] = str(len(rows))
        out["NumColumns"] = str(len(columns))
        out["NumColTitleRows"] = str(max(len(titles(t)) for t, _, _ in columns))
        out["ColDesc"] = [
            {"@colID": str(i + 1), "@dataType": data_type, "ColTitle": [{"@titleRow": str(k + 1), **({"@value": v} if v else {})} for k, v in enumerate(titles(t))], "ColType": type_}
            for i, (t, type_, data_type) in enumerate(columns)
        ]
        out["ReportData"] = _report_rows(rows)
        return out

    def _profit_and_loss(self, rng: dict[str, str], basis: str) -> dict:
        c = self.company
        totals: dict[str, float] = {}

        def add(account: str, v: float) -> None:
            totals[account] = money(totals.get(account, 0) + v)

        items = {i["FullName"]: i for i in c.lists.get("ItemInventory", [])}

        def in_range(r: Rec) -> bool:
            return rng["from"] <= text(get(r, "TxnDate")) <= rng["to"]

        for base in ("Invoice", "SalesReceipt"):
            for t in [x for x in c.txns.get(base, []) if in_range(x)]:
                for line in _arr(get(t, f"{base}LineRet")):
                    name = line["ItemRef"]["FullName"]
                    stock = items.get(name)
                    account = "Merchandise Sales" if stock else "Delivery Income" if name == "Delivery Fee" else "Installation Income"
                    add(account, line["Amount"])
                    if stock:
                        add("Cost of Goods Sold", to_num(get(line, "Quantity")) * to_num(get(stock, "AverageCost")))
        for base in ("Bill", "Check", "CreditCardCharge", "VendorCredit"):
            for t in [x for x in c.txns.get(base, []) if in_range(x)]:
                for line in _arr(get(t, "ExpenseLineRet")):
                    add(line["AccountRef"]["FullName"], -line["Amount"] if base == "VendorCredit" else line["Amount"])

        def of(type_: str) -> list[str]:
            return [a["Name"] for a in c.lists["Account"] if a.get("AccountType") == type_ and a["Name"] in totals]

        def total(names: list[str]) -> float:
            return money(add_up(totals.get(n, 0) for n in names))

        income, cogs, expense = of("Income"), of("CostOfGoodsSold"), of("Expense")
        gross = money(total(income) - total(cogs))
        net = money(gross - total(expense))

        def data(names: list[str]) -> list[dict]:
            return [{"kind": "data", "label": n, "rowType": "account", "values": [totals[n]]} for n in names]

        rows = [
            {"kind": "text", "label": "Ordinary Income/Expense"},
            {"kind": "text", "label": "Income"},
            *data(income),
            {"kind": "subtotal", "label": "Total Income", "rowType": "account", "values": [total(income)]},
            {"kind": "text", "label": "Cost of Goods Sold"},
            *data(cogs),
            {"kind": "subtotal", "label": "Total COGS", "rowType": "account", "values": [total(cogs)]},
            {"kind": "data", "label": "Gross Profit", "values": [gross]},
            {"kind": "text", "label": "Expense"},
            *data(expense),
            {"kind": "subtotal", "label": "Total Expense", "rowType": "account", "values": [total(expense)]},
            {"kind": "subtotal", "label": "Net Ordinary Income", "values": [net]},
            {"kind": "total", "label": "Net Income", "values": [net]},
        ]
        return self._report_xml("Profit & Loss", rng, basis, [("", "Label", "STRTYPE"), ("TOTAL", "Total", "AMTTYPE")], rows)

    def _sales_by_item(self, rng: dict[str, str]) -> dict:
        c = self.company
        qty: dict[str, float] = {}
        amt: dict[str, float] = {}
        for base in ("Invoice", "SalesReceipt"):
            for t in c.txns.get(base, []):
                if text(get(t, "TxnDate")) < rng["from"] or text(get(t, "TxnDate")) > rng["to"]:
                    continue
                for line in _arr(get(t, f"{base}LineRet")):
                    name = line["ItemRef"]["FullName"]
                    qty[name] = qty.get(name, 0) + to_num(get(line, "Quantity"))
                    amt[name] = money(amt.get(name, 0) + line["Amount"])
        stock = {i["FullName"] for i in c.lists.get("ItemInventory", [])}

        def group(label: str, names: list[str]) -> list[dict]:
            if not names:
                return []
            return [
                {"kind": "text", "label": label},
                *({"kind": "data", "label": n, "rowType": "item", "values": [qty[n], amt[n]]} for n in names),
                {"kind": "subtotal", "label": f"Total {label}", "values": [add_up(qty[n] for n in names), money(add_up(amt[n] for n in names))]},
            ]

        names = sorted(qty)
        rows = [
            *group("Inventory", [n for n in names if n in stock]),
            *group("Service", [n for n in names if n not in stock]),
            {"kind": "total", "label": "TOTAL", "values": [add_up(qty.values()), money(add_up(amt.values()))]},
        ]
        # QuickBooks titles the columns with the period on a first row, e.g. "Apr - Jun 25" over "Qty".
        period = f"{rng['from']} - {rng['to']}"
        return self._report_xml("Sales by Item Summary", rng, "Accrual", [("", "Label", "STRTYPE"), ([period, "Qty"], "Quantity", "QUANTYPE"), ([period, "Amount"], "Amount", "AMTTYPE")], rows)

    def _sales_by_customer(self, rng: dict[str, str]) -> dict:
        amt: dict[str, float] = {}
        for base in ("Invoice", "SalesReceipt"):
            for t in self.company.txns.get(base, []):
                if text(get(t, "TxnDate")) < rng["from"] or text(get(t, "TxnDate")) > rng["to"]:
                    continue
                name = t["CustomerRef"]["FullName"]
                amt[name] = money(amt.get(name, 0) + nz(amount_of(t), 0))
        rows = [{"kind": "data", "label": n, "rowType": "customer", "values": [amt[n]]} for n in sorted(amt)]
        rows.append({"kind": "total", "label": "TOTAL", "values": [money(add_up(amt.values()))]})
        return self._report_xml("Sales by Customer Summary", rng, "Accrual", [("", "Label", "STRTYPE"), ("TOTAL", "Total", "AMTTYPE")], rows)

    def _open_items(self, ar: bool, as_of: str) -> list[dict]:
        """What an aging lists on as_of: invoices and bills with something open, and payments and credits not applied yet (negative)."""
        out: list[dict] = []

        def add(type_: str, r: Rec, open_: Any, due: Any = MISSING) -> None:
            if truthy(open_) and text(get(r, "TxnDate")) <= as_of:
                out.append({
                    "type": type_, "id": text(get(r, "TxnID")), "ref": nz(_str(get(r, "RefNumber")), ""),
                    "party": text(get(nz(get(r, "CustomerRef"), get(r, "VendorRef")), "FullName")), "date": text(get(r, "TxnDate")), "due": due, "open": open_,
                })

        t = self.company.txns
        if ar:
            for i in t.get("Invoice", []):
                add("Invoice", i, get(i, "BalanceRemaining"), text(get(i, "DueDate")))
            for p in t.get("ReceivePayment", []):
                add("Payment", p, -to_num(get(p, "UnusedPayment")))
        else:
            for b in t.get("Bill", []):
                add("Bill", b, get(b, "OpenAmount"), text(get(b, "DueDate")))
            for v in t.get("VendorCredit", []):
                add("Credit", v, -to_num(get(v, "OpenAmount")))
        return out

    def _aging(self, type_: str, as_of: str) -> dict:
        ar = type_ == "ARAgingSummary"
        buckets: dict[str, list[float]] = {}
        for x in self._open_items(ar, as_of):
            i = _bucket(days_between(nz(x["due"], x["date"]), as_of))
            b = buckets.get(x["party"]) or [0, 0, 0, 0, 0]
            b[i] = money(b[i] + x["open"])
            buckets[x["party"]] = b
        rows = [{"kind": "data", "label": n, "rowType": "customer" if ar else "vendor", "values": [*buckets[n], money(add_up(buckets[n]))]} for n in sorted(buckets)]
        totals = [money(add_up(b[i] for b in buckets.values())) for i in range(5)]
        rows.append({"kind": "total", "label": "TOTAL", "values": [*totals, money(add_up(totals))]})
        cols = [("", "Label", "STRTYPE"), *((t, "Total" if t == "TOTAL" else "Amount", "AMTTYPE") for t in [*BUCKETS, "TOTAL"])]
        return self._report_xml("A/R Aging Summary" if ar else "A/P Aging Summary", {"from": as_of, "to": as_of}, None, cols, rows)

    def _aging_detail(self, type_: str, as_of: str, party: Any) -> dict:
        """One row per open item under each age bucket, laid out like QuickBooks' A/R and A/P Aging Detail."""
        ar = type_ == "ARAgingDetail"
        p = party.lower() if isinstance(party, str) else None
        items = [x for x in self._open_items(ar, as_of) if not p or x["party"].lower() == p or x["party"].lower().startswith(f"{p}:")]
        items.sort(key=lambda x: x["date"])

        def total(xs: list[dict]) -> list:
            return ["", "", "", "", "", "", money(add_up(x["open"] for x in xs)), ""]

        rows: list[dict] = []
        for i, label in enumerate(BUCKETS):
            xs = [x for x in items if _bucket(days_between(nz(x["due"], x["date"]), as_of)) == i]
            rows.append({"kind": "text", "label": label})
            for x in xs:
                late = days_between(x["due"], as_of) if truthy(x["due"]) else 0
                rows.append({"kind": "data", "label": "", "values": [x["type"], x["date"], x["ref"], x["party"], nz(x["due"], ""), late if late > 0 else "", x["open"], x["id"]]})
            rows.append({"kind": "subtotal", "label": f"Total {label}", "values": total(xs)})
        rows.append({"kind": "total", "label": "TOTAL", "values": total(items)})
        # As in QuickBooks, the first column has no title.
        cols = [
            ("", "Blank", "STRTYPE"), ("Type", "TxnType", "STRTYPE"), ("Date", "Date", "DATETYPE"), ("Num", "RefNumber", "STRTYPE"), ("Name", "Name", "STRTYPE"),
            ("Due Date", "DueDate", "DATETYPE"), ("Aging", "Aging", "INTTYPE"), ("Open Balance", "Amount", "AMTTYPE"), ("", "TxnID", "STRTYPE"),
        ]
        return self._report_xml("A/R Aging Detail" if ar else "A/P Aging Detail", {"from": as_of, "to": as_of}, "Accrual", cols, rows)

    # ---------------------------------------------------------------- writes

    def _items(self) -> list[Rec]:
        return [x for t in ITEM_TYPES for x in self.company.lists.get(t, [])]

    def _find_ref(self, base: str, ref_value: Any) -> Rec | None:
        r = ref_value
        if not truthy(r):
            return None
        pool = self._items() if base == "Item" else self.company.lists.get(base, [])
        for x in pool:
            if truthy(get(r, "ListID")) and x.get("ListID") == r["ListID"]:
                return x
            if truthy(get(r, "FullName")) and text(nz(get(x, "FullName"), get(x, "Name"))).lower() == text(r["FullName"]).lower():
                return x
        return None

    def _require_ref(self, base: str, ref_value: Any, where: str) -> Rec:
        found = self._find_ref(base, ref_value)
        if found is None:
            name = text(nz(get(ref_value, "FullName"), get(ref_value, "ListID"), ""))
            raise Status(3140, f'There is an invalid reference to QuickBooks {base} "{name}" in the {where}. QuickBooks error message: Invalid argument. The specified record does not exist in the list.')
        return found

    def _add(self, base: str, fields: Rec) -> Rec:
        c = self.company
        now = f"{c.today}T12:00:00"
        if base in ("Customer", "Vendor", *ITEM_TYPES):
            name = text(nz(get(fields, "Name"), ""))
            if not name:
                raise Status(3000, f"{base}Add needs a Name.")
            pool = self._items() if base.startswith("Item") else c.lists.get(base, [])
            if any(text(get(x, "Name")).lower() == name.lower() for x in pool):
                raise Status(3100, f'The name "{name}" of the list element is already in use.')
            rec = {
                "ListID": new_list_id(c), "TimeCreated": now, "TimeModified": now, "EditSequence": "1800000000", "Name": name,
                **({} if base == "Vendor" else {"FullName": name, "Sublevel": 0}), "IsActive": True, **fields, "Balance": 0,
            }
            c.lists.setdefault(base, []).append(rec)
            return rec
        party = nz(get(fields, "CustomerRef"), get(fields, "VendorRef"), get(fields, "PayeeEntityRef"))
        if truthy(get(fields, "CustomerRef")):
            self._require_ref("Customer", fields["CustomerRef"], base)
        if truthy(get(fields, "VendorRef")):
            self._require_ref("Vendor", fields["VendorRef"], base)
        rec = {"TxnID": new_txn_id(c), "TimeCreated": now, "TimeModified": now, "EditSequence": "1800000000", "TxnNumber": c.seq, "TxnDate": nz(get(fields, "TxnDate"), c.today)}
        for k, v in fields.items():
            if re.search(r"Line(Group)?Add\Z", k):
                rec[k[:-3] + "Ret"] = [self._line(base, line, i) for i, line in enumerate(_arr(v))]
            elif k != "AppliedToTxnAdd":
                rec[k] = v
        if truthy(get(party, "FullName")) and not truthy(rec.get("RefNumber")) and base == "Invoice":
            rec["RefNumber"] = str(1040 + len(c.txns.get("Invoice", [])) + 1)
        line_total = money(add_up(get(line, "Amount") if truthy(get(line, "Amount")) else 0 for k, v in list(rec.items()) if re.search(r"LineRet\Z", k) for line in _arr(v)))
        if base == "Invoice":
            rec.update({"Subtotal": line_total, "SalesTaxTotal": 0, "AppliedAmount": 0, "BalanceRemaining": line_total, "IsPaid": False})
            if not truthy(rec.get("DueDate")):
                rec["DueDate"] = add_days(text(rec["TxnDate"]), 30)
        elif base == "Bill":
            rec.update({"AmountDue": line_total, "OpenAmount": line_total, "IsPaid": False})
            if not truthy(rec.get("DueDate")):
                rec["DueDate"] = add_days(text(rec["TxnDate"]), 30)
        elif base == "ReceivePayment":
            rec["TotalAmount"] = to_num(nz(get(fields, "TotalAmount"), 0))
            rec["AppliedToTxnRet"] = self._apply_payment(rec, fields)
        elif base == "CreditMemo":
            rec.update({"Subtotal": line_total, "TotalAmount": line_total, "CreditRemaining": line_total})
        elif truthy(line_total):
            rec.update({"Subtotal": line_total, "TotalAmount": line_total})
        c.txns.setdefault(base, []).append(rec)
        recompute_balances(c)
        return rec

    def _line(self, base: str, line: Any, i: int) -> Rec:
        line = _dict(line)
        out: Rec = {"TxnLineID": f"{self.company.seq}-{i + 1}"}
        self.company.seq += 1
        for k, v in line.items():
            if k != "TxnLineID":
                out[k] = v
        if truthy(get(line, "ItemRef")):
            item = self._require_ref("Item", line["ItemRef"], f"{base} line")
            out["ItemRef"] = {"ListID": get(item, "ListID"), "FullName": get(item, "FullName")}
        if truthy(get(line, "AccountRef")):
            acct = self._require_ref("Account", line["AccountRef"], f"{base} line")
            out["AccountRef"] = {"ListID": get(acct, "ListID"), "FullName": get(acct, "FullName")}
        if get(out, "Amount") is MISSING:
            rate = to_num(nz(get(line, "Rate"), get(line, "Cost"), 0))
            out["Amount"] = money(to_num(nz(get(line, "Quantity"), 1)) * rate)
        else:
            out["Amount"] = to_num(out["Amount"])
        if get(line, "Quantity") is not MISSING:
            out["Quantity"] = to_num(line["Quantity"])
        return out

    def _apply_payment(self, rec: Rec, fields: Rec) -> list[Rec]:
        customer = get(self._find_ref("Customer", get(rec, "CustomerRef")), "FullName")
        invoices = [i for i in self.company.txns.get("Invoice", []) if not truthy(i.get("IsPaid")) and get(i["CustomerRef"], "FullName") == customer]
        wanted = _arr(get(fields, "AppliedToTxnAdd"))
        left = rec["TotalAmount"]
        applied: list[Rec] = []

        def apply(inv: Rec, amount: float) -> None:
            nonlocal left
            amt = money(_js_min(amount, to_num(inv["BalanceRemaining"]), left))
            if amt <= 0:
                return
            inv["BalanceRemaining"] = money(inv["BalanceRemaining"] - amt)
            inv["AppliedAmount"] = money(inv["AppliedAmount"] - amt)
            inv["IsPaid"] = inv["BalanceRemaining"] <= 0
            left = money(left - amt)
            applied.append({"TxnID": inv["TxnID"], "TxnType": "Invoice", "TxnDate": inv["TxnDate"], "RefNumber": get(inv, "RefNumber"), "Amount": amt})

        if wanted:
            for w in wanted:
                inv = next((i for i in invoices if i.get("TxnID") == get(w, "TxnID")), None)
                if inv is None:
                    raise Status(3120, f'Object "{text(get(w, "TxnID"))}" specified in the request cannot be found.')
                apply(inv, to_num(nz(get(w, "PaymentAmount"), left)))
        elif text(get(fields, "IsAutoApply")) == "true":
            for inv in sorted(invoices, key=lambda i: text(get(i, "TxnDate"))):
                apply(inv, left)
        rec["UnusedPayment"] = left
        rec["UnusedCredits"] = 0
        return applied

    def _mod(self, base: str, fields: Rec) -> Rec:
        c = self.company
        is_list = not truthy(get(fields, "TxnID"))
        pool = (self._items() if base.startswith("Item") else c.lists.get(base, [])) if is_list else c.txns.get(base, [])
        key_field = "ListID" if is_list else "TxnID"
        rec = next((x for x in pool if get(x, key_field) == get(fields, key_field)), None)
        if rec is None:
            raise Status(3120, f'Object "{text(nz(get(fields, "ListID"), get(fields, "TxnID")))}" specified in the request cannot be found.')
        if text(get(rec, "EditSequence")) != text(get(fields, "EditSequence")):
            raise Status(3200, "The provided edit sequence is out-of-date.")
        for k, v in fields.items():
            if k in ("ListID", "TxnID", "EditSequence"):
                continue
            if re.search(r"LineMod\Z", k):
                key = k[:-3] + "Ret"
                old = _arr(get(rec, key))
                lines = []
                for i, line in enumerate(_arr(v)):
                    line = _dict(line)
                    prev = next((o for o in old if get(o, "TxnLineID") == get(line, "TxnLineID")), None)
                    lines.append({**prev, **self._line(base, {**prev, **line}, i), "TxnLineID": get(prev, "TxnLineID")} if prev is not None else self._line(base, line, i))
                rec[key] = lines
            else:
                rec[k] = v
        rec["EditSequence"] = text(to_num(get(rec, "EditSequence")) + 1)
        rec["TimeModified"] = f"{c.today}T12:00:00"
        if base == "Invoice":
            sub = money(add_up(to_num(get(line, "Amount")) for line in _arr(get(rec, "InvoiceLineRet"))))
            rec["Subtotal"] = sub
            rec["BalanceRemaining"] = money(sub + to_num(get(rec, "AppliedAmount")))
        recompute_balances(c)
        return rec

    def _delete_txn(self, message: str, rq: Rec) -> Rec:
        c = self.company
        type_ = text(nz(get(rq, "TxnDelType"), get(rq, "TxnVoidType")))
        rows = c.txns.get(type_, [])
        i = next((j for j, x in enumerate(rows) if x.get("TxnID") == get(rq, "TxnID")), -1)
        if i < 0:
            raise Status(3120, f'Object "{text(get(rq, "TxnID"))}" specified in the request cannot be found.')
        rec = rows[i]
        if message == "TxnDelRq":
            rows.pop(i)
            recompute_balances(c)
            ret = {"TxnDelType": type_, "TxnID": rec["TxnID"], "TimeCreated": rec.get("TimeCreated"), "TimeDeleted": f"{c.today}T12:00:00", "RefNumber": get(rec, "RefNumber")}
            self.deleted["TxnDeletedRet"].append(ret)
            return {k: v for k, v in ret.items() if k != "TimeCreated"}
        for k in ("Subtotal", "TotalAmount", "AmountDue", "OpenAmount", "BalanceRemaining", "Amount"):
            if k in rec:
                rec[k] = 0
        rec["Memo"] = f"VOID: {text(nz(get(rec, 'Memo'), ''))}".strip()
        recompute_balances(c)
        return {"TxnVoidType": type_, "TxnID": rec["TxnID"]}

    def _delete_list(self, rq: Rec) -> Rec:
        c = self.company
        type_ = text(get(rq, "ListDelType"))
        rows = c.lists.get(type_, [])
        i = next((j for j, x in enumerate(rows) if x.get("ListID") == get(rq, "ListID")), -1)
        if i < 0:
            raise Status(3120, f'Object "{text(get(rq, "ListID"))}" specified in the request cannot be found.')
        rec = rows.pop(i)
        ret = {"ListDelType": type_, "ListID": rec["ListID"], "TimeDeleted": f"{c.today}T12:00:00", "FullName": nz(get(rec, "FullName"), get(rec, "Name"))}
        self.deleted["ListDeletedRet"].append({**ret, "TimeCreated": rec.get("TimeCreated")})
        return ret
