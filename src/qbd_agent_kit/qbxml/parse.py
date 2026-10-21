"""qbXML -> JSON. Repeating elements always become lists and numbers, booleans and dates get their
real types, using the schema; IDs and reference numbers stay text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import QbxmlParseError
from ..jsutil import MISSING, finite, parse_int, text, to_number
from . import xml
from .schema import is_model, message_model, model


@dataclass
class QbResponse:
    message: str                 # e.g. "InvoiceQueryRs"
    request_id: str | None
    status_code: float           # 0 ok, 1 no match, 500-ish warnings, 3000+ errors
    status_severity: str         # Info, Warn, Error
    status_message: str
    attrs: dict[str, str]        # iteratorID, iteratorRemainingCount, retCount...
    body: dict[str, Any]         # normalized content, e.g. {"InvoiceRet": [...]}


def coerce(code: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value == "":
        return None
    if code in ("a", "p", "q", "r", "f"):
        n = to_number(value)
        return n if finite(n) else value
    if code == "i":
        n = parse_int(value)
        return n if finite(n) else value
    if code == "b":
        return value == "true"
    return value


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else [v]


def normalize(node: Any, ref: str | None) -> Any:
    if ref is None:
        return node
    if not is_model(ref):
        if isinstance(node, dict) and "#text" in node:
            return coerce(ref, node["#text"])
        return coerce(ref, node)
    m = model(ref)
    if m is None or not isinstance(node, dict):
        return None if node == "" else node
    out = {}
    for key, value in node.items():
        child = m.c.get(key)
        if child is None:
            out[key] = value
            continue
        child_ref, repeats = child
        out[key] = [normalize(v, child_ref) for v in _as_list(value)] if repeats else normalize(value, child_ref)
    return out


def _status(attrs: dict[str, str]) -> float:
    n = to_number(attrs.get("statusCode", "0"))
    return int(n) if finite(n) and float(n).is_integer() else n


def parse_responses(text_: str) -> list[QbResponse]:
    """Every response in a QBXMLMsgsRs envelope, in document order per message type."""
    doc = xml.parse(text_)
    qbxml = doc.get("QBXML")
    rs = qbxml.get("QBXMLMsgsRs") if isinstance(qbxml, dict) else None
    if not isinstance(rs, dict):
        raise QbxmlParseError("QuickBooks returned no QBXMLMsgsRs element")
    out = []
    for message, value in rs.items():
        if message.startswith("@"):
            continue
        m = message_model(message)
        for item in _as_list(value):
            node = item if isinstance(item, dict) else {}
            attrs: dict[str, str] = {}
            content: dict[str, Any] = {}
            for k, v in node.items():
                if k.startswith("@"):
                    attrs[k[1:]] = text(v)
                else:
                    content[k] = v
            body = content
            if m is not None:
                body = {}
                for k, v in content.items():
                    child = m.c.get(k)
                    if child is None:
                        body[k] = v
                    else:
                        body[k] = [normalize(x, child[0]) for x in _as_list(v)] if child[1] else normalize(v, child[0])
            out.append(QbResponse(
                message=message,
                request_id=attrs.get("requestID"),
                status_code=_status(attrs),
                status_severity=attrs.get("statusSeverity", "Info"),
                status_message=attrs.get("statusMessage", ""),
                attrs=attrs,
                body=body,
            ))
    return out


# ---------------------------------------------------------------- reports


@dataclass
class ReportColumn:
    id: str
    title: str
    type: str        # ColType, e.g. Label, Amount, Total
    data_type: str   # qbXML data type of the values, e.g. AMTTYPE


@dataclass
class ReportRow:
    kind: str        # data, text, subtotal, total
    depth: int
    label: str
    values: list     # one per report column, in column order
    row_type: Any = MISSING   # e.g. account, customer, item


@dataclass
class Report:
    request_id: str | None
    status_code: float
    status_message: str
    title: str = ""
    subtitle: str = ""
    basis: Any = MISSING
    columns: list[ReportColumn] = field(default_factory=list)
    rows: list[ReportRow] = field(default_factory=list)


NUMERIC = {"AMTTYPE", "PRICETYPE", "QUANTYPE", "PERCENTTYPE", "INTTYPE", "FLOATTYPE"}


def parse_reports(text_: str) -> list[Report]:
    """Reports keep their row order (text, data, subtotal and total rows interleave)."""
    root = xml.read(text_)
    qbxml = root.find("QBXML")
    msgs = qbxml.find("QBXMLMsgsRs") if qbxml else None
    reports = []
    for rs in msgs.elements() if msgs else []:
        if not rs.tag.endswith("ReportQueryRs"):
            continue
        a = rs.attrs
        report = Report(request_id=a.get("requestID"), status_code=_status(a), status_message=a.get("statusMessage", ""))
        ret = rs.find("ReportRet")
        if ret is not None:
            _fill_report(report, ret)
        reports.append(report)
    return reports


def _fill_report(report: Report, ret: xml.Element) -> None:
    for e in ret.elements():
        if e.tag == "ReportTitle":
            report.title = e.text()
        elif e.tag == "ReportSubtitle":
            report.subtitle = e.text()
        elif e.tag == "ReportBasis":
            report.basis = e.text()
        elif e.tag == "ColDesc":
            titles = [k.attrs.get("value", "") for k in e.elements() if k.tag == "ColTitle"]
            col_type = e.find("ColType")
            report.columns.append(ReportColumn(
                id=e.attrs.get("colID", MISSING),
                title=" ".join(t for t in titles if t).strip(),
                type=col_type.text() if col_type is not None else "",
                data_type=e.attrs.get("dataType", "STRTYPE"),
            ))
        elif e.tag == "ReportData":
            _fill_rows(report, e)


def _fill_rows(report: Report, data: xml.Element) -> None:
    index: dict[Any, int] = {}
    for i, c in enumerate(report.columns):
        index[c.id] = i
    depth = 0
    for e in data.elements():
        if e.tag == "TextRow":
            report.rows.append(ReportRow("text", depth, e.attrs.get("value", ""), []))
            depth += 1
            continue
        if e.tag not in ("DataRow", "SubtotalRow", "TotalRow"):
            continue
        if e.tag == "SubtotalRow" and depth > 0:
            depth -= 1
        kind = "data" if e.tag == "DataRow" else "subtotal" if e.tag == "SubtotalRow" else "total"
        row = ReportRow(kind, depth, "", [None] * len(report.columns))
        for k in e.elements():
            if k.tag == "RowData":
                row.row_type = k.attrs.get("rowType", MISSING)
                row.label = k.attrs.get("value", "")
            elif k.tag == "ColData":
                i = index.get(k.attrs.get("colID", MISSING))
                if i is None:
                    continue
                raw = k.attrs.get("value")
                numeric = raw is not None and raw != "" and report.columns[i].data_type in NUMERIC and finite(to_number(raw))
                row.values[i] = to_number(raw) if numeric else raw
        if not row.label and isinstance(row.values[0] if row.values else None, str):
            row.label = row.values[0]
        report.rows.append(row)


def col_index(report: Report, title: str) -> int:
    """Index of the first column whose title matches the regular expression (case-insensitive). -1 if none."""
    pattern = re.compile(title, re.I)
    for i, c in enumerate(report.columns):
        if pattern.search(c.title):
            return i
    return -1
