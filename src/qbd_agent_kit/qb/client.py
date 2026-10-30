"""Requests to QuickBooks: picks the qbXML version, pages through big results with iterators, and turns
status errors into exceptions."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import QbStatusError
from ..jsutil import MISSING, finite, nz, text, to_fixed, to_number
from ..qbxml.build import build_request, envelope
from ..qbxml.parse import QbResponse, Report, parse_reports, parse_responses
from ..qbxml.schema import message_model, supports_iterator

SCHEMA_VERSION = "16.0"  # the schema this kit is generated from; newer QuickBooks still gets requests in this version
PROBE_VERSION = "13.0"   # qbXML 13.0 = QuickBooks 2014; every supported QuickBooks Desktop understands it
REQUEST_NAME = re.compile(r"<(?!QBXMLMsgsRq\b)(\w+Rq)\b")


class Transport(Protocol):
    kind: str

    def open(self) -> dict[str, Any]:
        """Opens the QuickBooks session if needed and returns {"companyFile": ...}. Safe to call before every request."""

    def process(self, qbxml: str) -> str:
        """Sends one qbXML envelope and returns QuickBooks' raw answer."""

    def close(self) -> None:
        """Ends the session."""


@dataclass
class HostInfo:
    product_name: str
    version: str
    country: str
    supported_versions: list[str]
    qbxml_version: str
    file_mode: Any
    company_file: str


@dataclass
class QueryResult:
    rows: list[dict]
    truncated: bool            # more records matched than were returned
    warnings: list[str] = field(default_factory=list)
    remaining: Any = MISSING   # how many more, when QuickBooks says


class QbClient:
    def __init__(self, transport: Transport, qbxml_version: str | None = None):
        self.transport = transport
        self._forced_version = qbxml_version
        self._host: HostInfo | None = None
        self._session: dict[str, Any] | None = None
        self._next_id = 1
        self.calls: list[dict[str, Any]] = []  # every envelope sent, for the call log: request, ms, status, items
        self.watch: Any = None                 # the command's live file (safety.live.Live), told about each request
        self._paging: dict[str, Any] = {}      # where a paged read is, for the live file

    @property
    def company_file(self) -> str | None:
        """The company file QuickBooks opened for this command, once it has."""
        return self._session.get("companyFile") if self._session else None

    @property
    def host(self) -> HostInfo | None:
        return self._host

    def _send(self, qbxml: str, paging: dict[str, Any] | None = None) -> str:
        entry: dict[str, Any] = {"request": ",".join(REQUEST_NAME.findall(qbxml))}
        if self.watch is not None:
            self.watch.step(entry["request"], self._paging if paging is None else paging)  # stops here when a person asked the monitor to
        self.calls.append(entry)
        start = time.monotonic()
        try:
            answer = self.transport.process(qbxml)
        finally:
            entry["ms"] = round((time.monotonic() - start) * 1000)
        entry["bytes"] = len(answer)
        return answer

    def _note(self, body: dict, status: Any, items: int, **extra: Any) -> None:
        """What the last request asked for (its element names, not their values) and how many records came back."""
        if self.calls:
            self.calls[-1].update(elements=[k for k in body if k != "MaxReturned"], status=status, items=items, **extra)

    def host_info(self) -> HostInfo:
        """Product, version and the qbXML version used for every request. Asked once per session."""
        session = self.transport.open()
        if self._host and self._session and self._session.get("companyFile") == session.get("companyFile"):
            return self._host
        self._session = session
        if self.watch is not None:
            self.watch.connected(session.get("companyFile", ""))
        raw = self._send(envelope([build_request("HostQueryRq", {}, {"requestID": "host"})], PROBE_VERSION), {})
        answers = parse_responses(raw)
        rs = answers[0] if answers else None
        if rs is None or rs.status_severity == "Error":
            raise QbStatusError(rs.status_code if rs else -1, rs.status_message if rs else "HostQuery failed")
        ret = rs.body.get("HostRet")
        ret = ret if isinstance(ret, dict) else {}
        supported = [text(v) for v in nz(ret.get("SupportedQBXMLVersion", MISSING), [])]
        self._host = HostInfo(
            product_name=text(nz(ret.get("ProductName", MISSING), "QuickBooks")),
            version=f"{text(nz(ret.get('MajorVersion', MISSING), '?'))}.{text(nz(ret.get('MinorVersion', MISSING), '?'))}",
            country=text(nz(ret.get("Country", MISSING), "")),
            supported_versions=supported,
            qbxml_version=self._forced_version or pick_version(supported),
            file_mode=ret.get("QBFileMode", MISSING),
            company_file=session.get("companyFile", ""),
        )
        return self._host

    def raw(self, requests: list[str], on_error: str = "stopOnError") -> str:
        """Sends request elements in one envelope and returns QuickBooks' raw XML answer."""
        version = self.host_info().qbxml_version
        return self._send(envelope(requests, version, on_error))

    def request(self, message: str, body: dict, attrs: dict[str, Any] | None = None) -> QbResponse:
        """One request; raises QbStatusError when QuickBooks answers with an error status."""
        request_id = str(self._next_id)
        self._next_id += 1
        answer = self.raw([build_request(message, body, {"requestID": request_id, **(attrs or {})})])
        rs = next((r for r in parse_responses(answer) if r.request_id == request_id), None)
        if rs is None:
            raise QbStatusError(-1, f"QuickBooks sent no answer to {message}")
        self._note(body, rs.status_code, sum(len(v) if isinstance(v, list) else 1 for k, v in rs.body.items() if k.endswith("Ret")))
        if rs.status_severity == "Error":
            raise QbStatusError(rs.status_code, rs.status_message)
        return rs

    def query(self, message: str, body: dict, limit: int, page_size: int = 200) -> QueryResult:
        """A query that stops at `limit` records. Queries that support iterators are fetched in pages,
        so a big company file is never asked for everything at once."""
        warnings: list[str] = []
        m = message_model(message)
        has_max = m is not None and "MaxReturned" in m.c
        has_ids = any(body.get(k, MISSING) is not MISSING for k in ("ListID", "FullName", "TxnID", "RefNumber"))
        if not supports_iterator(message) or limit <= page_size or has_ids:
            rs = self.request(message, {**body, "MaxReturned": limit + 1} if has_max and not has_ids else body)
            if rs.status_severity == "Warn":
                warnings.append(rs.status_message)
            rows = rets_of(rs)
            return QueryResult(rows=rows[:limit], truncated=len(rows) > limit, warnings=warnings)
        rows: list[dict] = []
        iterator_id: str | None = None
        remaining: float = 0
        page = 0
        try:
            while True:
                attrs = {"iterator": "Continue", "iteratorID": iterator_id} if iterator_id else {"iterator": "Start"}
                page += 1
                # QuickBooks says how many records are left after each page, so the monitor can show real progress.
                self._paging = {"page": page, "page_size": page_size, "read": len(rows), "cap": limit, **({"left": int(remaining)} if page > 1 else {})}
                rs = self.request(message, {**body, "MaxReturned": min(page_size, limit - len(rows))}, attrs)
                if rs.status_severity == "Warn":
                    warnings.append(rs.status_message)
                rows.extend(rets_of(rs))
                iterator_id = rs.attrs.get("iteratorID")
                remaining = to_number(rs.attrs.get("iteratorRemainingCount", "0"))
                if self.watch is not None:
                    self.watch.answered(read=len(rows), left=int(remaining) if finite(remaining) else 0)
                if not (remaining > 0 and len(rows) < limit and iterator_id):
                    break
        finally:
            self._paging = {}
        if remaining > 0 and iterator_id:
            # Free the iterator inside QuickBooks instead of waiting for the session to end.
            try:
                self.request(message, {**body, "MaxReturned": 1}, {"iterator": "Stop", "iteratorID": iterator_id})
            except Exception:
                pass
        return QueryResult(rows=rows, truncated=remaining > 0, warnings=warnings, remaining=remaining if finite(remaining) and remaining else MISSING)

    def report(self, message: str, body: dict) -> Report:
        request_id = str(self._next_id)
        self._next_id += 1
        report_type = next((v for k, v in body.items() if k.endswith("ReportType")), MISSING)
        self._paging = {"report": report_type} if isinstance(report_type, str) else {}
        try:
            answer = self.raw([build_request(message, body, {"requestID": request_id})])
        finally:
            self._paging = {}
        rs = next((r for r in parse_responses(answer) if r.request_id == request_id), None)
        report = next((r for r in parse_reports(answer) if r.request_id == request_id), None)
        self._note(body, rs.status_code if rs else -1, len(report.rows) if report else 0, report=report_type)
        if rs is not None and rs.status_severity == "Error":
            raise QbStatusError(rs.status_code, rs.status_message)
        if report is None:
            raise QbStatusError(rs.status_code if rs else -1, (rs.status_message if rs else "") or f"QuickBooks sent no {message} report")
        return report


def pick_version(supported: list[str]) -> str:
    """Highest plain qbXML version QuickBooks supports, capped at the schema this kit knows."""
    plain = [v for v in (to_number(s) for s in supported) if finite(v) and 0 < v <= float(SCHEMA_VERSION)]
    return to_fixed(max(plain), 1) if plain else PROBE_VERSION


def rets_of(rs: QbResponse) -> list[dict]:
    """Every *Ret record in a query answer. Mixed answers (ItemQuery) get a _type such as "ItemInventory"."""
    m = message_model(rs.message)
    mixed = m is not None and sum(1 for k in m.c if k.endswith("Ret")) > 1
    out = []
    for k, v in rs.body.items():
        if not k.endswith("Ret"):
            continue
        for r in v if isinstance(v, list) else [v]:
            if isinstance(r, dict):
                out.append({"_type": k[:-3], **r} if mixed else r)
    return out
