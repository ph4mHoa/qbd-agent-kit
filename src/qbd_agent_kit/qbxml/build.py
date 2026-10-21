"""JSON -> qbXML. Children are written in schema order, because QuickBooks rejects a request whose
elements are out of order with a bare parse error."""
from __future__ import annotations

import math
import re
from typing import Any

from ..errors import QbxmlInputError
from ..jsutil import MISSING, _WS, dumps, finite, is_num, num_str, text, to_fixed, to_number
from .schema import Model, is_model, message_model, model

LIST_ID = re.compile(r"[0-9A-Fa-f]{1,8}-[0-9]{9,10}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:+\-]+)?\Z")
_NON_ASCII = re.compile(r"[^\x00-\x7F]")


def escape_xml(s: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    # QuickBooks fails to parse a request with non-ASCII characters (0x80040400); numeric references get through.
    return _NON_ASCII.sub(lambda m: f"&#{ord(m.group(0))};", s)


def shown(v: Any) -> str:
    """A value quoted in an error message, as JSON."""
    return "undefined" if v is MISSING else dumps(v)


def _to_number(v: Any, path: str) -> float:
    if is_num(v):
        n = v
    elif isinstance(v, str) and v.strip(_WS) != "":
        n = to_number(v)
    else:
        n = math.nan
    if not finite(n):
        raise QbxmlInputError(f"{path}: expected a number, got {shown(v)}")
    return n


def _trim_decimals(n: float, places: int) -> str:
    s = re.sub(r"(\.[0-9]*?)0+\Z", r"\1", to_fixed(n, places))
    s = s[:-1] if s.endswith(".") else s
    return "0" if s == "-0" else s


def format_value(code: str, v: Any, path: str, limit: int | None = None) -> str:
    if code == "b":
        if isinstance(v, bool):
            return "true" if v else "false"
        if v in ("true", "false"):
            return v
        raise QbxmlInputError(f"{path}: expected true or false, got {shown(v)}")
    if code == "a":
        return to_fixed(_to_number(v, path), 2)
    if code in ("p", "q", "r"):
        return _trim_decimals(_to_number(v, path), 5)
    if code == "i":
        n = _to_number(v, path)
        if not float(n).is_integer():
            raise QbxmlInputError(f"{path}: expected a whole number, got {num_str(n)}")
        return num_str(n)
    if code == "f":
        return num_str(_to_number(v, path))
    if code == "d":
        if isinstance(v, str) and _DATE.match(v):
            return v
        raise QbxmlInputError(f"{path}: expected a date like 2026-10-31, got {shown(v)}")
    if code == "t":
        if isinstance(v, str) and _DATETIME.match(v):
            return v
        raise QbxmlInputError(f"{path}: expected a date-time like 2026-10-31T09:00:00, got {shown(v)}")
    if isinstance(v, str) or is_num(v):
        s = text(v)
        if limit is not None and len(s) > limit:
            # QuickBooks would refuse it only on commit (status 3070), after the person approved the preview.
            raise QbxmlInputError(f"{path}: {shown(v)} has {len(s)} characters; QuickBooks takes at most {limit} here")
        return s
    raise QbxmlInputError(f"{path}: expected text, got {shown(v)}")


def _attributes(obj: dict) -> str:
    return "".join(f' {k[1:]}="{escape_xml(text(v))}"' for k, v in obj.items() if k.startswith("@") and v is not None and v is not MISSING)


def _expand_shorthand(m: Model, value: Any) -> Any:
    """Shorthand for references: "Acme Ltd" -> {FullName}, "80000001-1234567890" -> {ListID}."""
    if not isinstance(value, str):
        return value
    if "ListID" in m.c and LIST_ID.match(value):
        return {"ListID": value}
    if "FullName" in m.c:
        return {"FullName": value}
    if "TxnID" in m.c:
        return {"TxnID": value}
    return value


def build_children(m: Model, obj: dict, path: str) -> str:
    for key in obj:
        if key.startswith("@") or key == "#text":
            continue
        if key not in m.c:
            raise QbxmlInputError(f'{path}: unknown element "{key}". Allowed here: {", ".join(m.o)}')
    out = []
    for name in m.o:
        raw = obj.get(name)
        if raw is None or raw is MISSING:
            continue
        ref, repeats = m.c[name]
        values = raw if isinstance(raw, list) else [raw]
        if not repeats and len(values) > 1:
            raise QbxmlInputError(f"{path}/{name}: only one value is allowed")
        for v in values:
            out.append(build_element(name, v, ref, f"{path}/{name}", m.l.get(name)))
    return "".join(out)


def build_element(name: str, value: Any, ref: str, path: str, limit: int | None = None) -> str:
    if not is_model(ref):
        if isinstance(value, dict):
            return f"<{name}{_attributes(value)}>{escape_xml(format_value(ref, value.get('#text', MISSING), path, limit))}</{name}>"
        return f"<{name}>{escape_xml(format_value(ref, value, path, limit))}</{name}>"
    m = model(ref)
    if m is None:
        raise QbxmlInputError(f"{path}: no schema for this element")
    v = _expand_shorthand(m, value)
    if not isinstance(v, dict):
        raise QbxmlInputError(f"{path}: expected an object with {', '.join(m.o)}")
    return f"<{name}{_attributes(v)}>{build_children(m, v, path)}</{name}>"


def build_request(message: str, body: dict, attrs: dict[str, Any] | None = None) -> str:
    """One request element, e.g. build_request("InvoiceQueryRq", {"MaxReturned": 5}, {"requestID": "1"})."""
    m = message_model(message)
    if m is None:
        raise QbxmlInputError(f"QuickBooks has no request called {message}")
    a = "".join(f' {k}="{escape_xml(text(v))}"' for k, v in (attrs or {}).items() if v is not None and v is not MISSING)
    return f"<{message}{a}>{build_children(m, body, message)}</{message}>"


def envelope(requests: list[str], version: str, on_error: str = "continueOnError") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n<?qbxml version="{version}"?>\n'
        f'<QBXML><QBXMLMsgsRq onError="{on_error}">{"".join(requests)}</QBXMLMsgsRq></QBXML>'
    )
