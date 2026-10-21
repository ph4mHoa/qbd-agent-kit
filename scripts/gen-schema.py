#!/usr/bin/env python3
"""Generate src/qbd_agent_kit/qbxml/schema.json from the QuickBooks SDK's qbXML schema.

The JSON holds what the kit needs and nothing more: for each request and response
message, the order of child elements (qbXML is order-sensitive), which children
repeat, a coarse value type (string, number, boolean, date) for coercion, and the
most characters QuickBooks takes in each text field.

The schema files come with the QuickBooks Desktop SDK (free download from Intuit's
developer site). They are not part of this repository.

usage: python3 scripts/gen-schema.py <dir with qbxmlops160.xsd> [out.json]
"""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

XS = "{http://www.w3.org/2001/XMLSchema}"
# Value codes: s string, b boolean, d date, t date-time, a amount (2 decimals), p price,
# q quantity, r percent (up to 5 decimals), i integer, f float.
CODES = {"BOOLTYPE": "b", "DATETYPE": "d", "DATETIMETYPE": "t", "AMTTYPE": "a", "PRICETYPE": "p",
         "QUANTYPE": "q", "PERCENTTYPE": "r", "INTTYPE": "i", "FLOATTYPE": "f"}

src = Path(sys.argv[1])
out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "src/qbd_agent_kit/qbxml/schema.json"

elements, ctypes, groups, stypes = {}, {}, {}, {}
for name in ("qbxmltypes160.xsd", "qbxml160.xsd", "qbxmlso160.xsd", "qbxmlops160.xsd"):
    root = ET.parse(src / name).getroot()
    for child in root:
        n = child.get("name")
        if child.tag == XS + "element":
            elements[n] = child
        elif child.tag == XS + "complexType":
            ctypes[n] = child
        elif child.tag == XS + "group":
            groups[n] = child
        elif child.tag == XS + "simpleType":
            stypes[n] = child


def simple_code(type_name):
    """Map a named simple type to a value code (see CODES)."""
    seen = set()
    while type_name and type_name not in seen:
        seen.add(type_name)
        if type_name in CODES:
            return CODES[type_name]
        node = stypes.get(type_name)
        if node is None:
            return "s"
        restr = node.find(XS + "restriction")
        if restr is None:
            return "s"
        type_name = restr.get("base")
    return "s"


def inline_simple_code(st):
    restr = st.find(XS + "restriction")
    return simple_code(restr.get("base")) if restr is not None else "s"


def max_length(el):
    """The maxLength on an xsd:element's text (inline, through a ref or through named simple types), or None."""
    if el.get("ref"):
        return max_length(elements[el.get("ref")])
    node = el.find(XS + "simpleType")
    if node is None:
        node = stypes.get(el.get("type"))
    seen = set()
    while node is not None:
        restr = node.find(XS + "restriction")
        if restr is None:
            return None
        ml = restr.find(XS + "maxLength")
        if ml is not None:
            return int(ml.get("value"))
        if restr.get("base") in seen:
            return None
        seen.add(restr.get("base"))
        node = stypes.get(restr.get("base"))
    return None


def occurs(node):
    mx = node.get("maxOccurs", "1")
    return int(node.get("minOccurs", "1")), (10**9 if mx == "unbounded" else int(mx))


models = {}       # id -> {"o": [...], "c": {name: [ref, repeat]}, "l": {name: max length of its text}}
memo = {}         # global element name -> ref


def store(model):
    key = json.dumps(model, sort_keys=True)
    mid = "m" + hashlib.sha1(key.encode()).hexdigest()[:10]
    models.setdefault(mid, model)
    return mid


def element_ref(el):
    """Return a value code ('s', 'n', ...) or a model id for an xsd:element node."""
    if el.get("ref"):
        name = el.get("ref")
        if name in memo:
            return memo[name]
        memo[name] = "s"  # guard against recursion; replaced below
        ref = element_ref(elements[name])
        memo[name] = ref
        return ref
    if el.get("type"):
        t = el.get("type")
        return content_model(ctypes[t]) if t in ctypes else simple_code(t)
    ct = el.find(XS + "complexType")
    if ct is not None:
        return content_model(ct)
    st = el.find(XS + "simpleType")
    return inline_simple_code(st) if st is not None else "s"


def content_model(ct):
    sc = ct.find(XS + "simpleContent")
    if sc is not None:
        ext = sc.find(XS + "extension")
        return simple_code(ext.get("base")) if ext is not None else "s"
    order, children, lengths = [], {}, {}

    def walk(node, repeat):
        for p in node:
            if p.tag in (XS + "annotation", XS + "attribute"):
                continue
            _, mx = occurs(p)
            rep = repeat or mx > 1
            if p.tag == XS + "element":
                name = p.get("ref") or p.get("name")
                ref = element_ref(p)
                if name not in children:
                    order.append(name)
                    children[name] = [ref, 1 if rep else 0]
                    ml = max_length(p) if ref == "s" else None
                    if ml:
                        lengths[name] = ml
                elif rep:
                    children[name][1] = 1
            elif p.tag in (XS + "sequence", XS + "choice"):
                walk(p, rep)
            elif p.tag == XS + "group":
                walk(groups[p.get("ref")], rep)

    walk(ct, False)
    if not order:
        return "s"
    return store({"o": order, "c": children, **({"l": lengths} if lengths else {})})


roots, iterators, counts = {}, [], []
for name, ct in sorted(ctypes.items()):
    if name.endswith("RqType") or name.endswith("RsType"):
        roots[name[:-4]] = content_model(ct)
        attrs = {a.get("name") for a in ct.findall(XS + "attribute")}
        if "iterator" in attrs:
            iterators.append(name[:-4])
        if "metaData" in attrs:
            counts.append(name[:-4])

# Enumerations used by tools (report types, enums the server validates against).
enums = {}
for name in ("GeneralSummaryReportType", "GeneralDetailReportType", "AgingReportType", "BudgetSummaryReportType",
             "JobReportType", "TimeReportType", "TxnDelType", "TxnVoidType", "ListDelType", "TxnType",
             "ReportBasis", "SummarizeColumnsBy", "DateMacro", "AccountType"):
    node = elements.get(name)
    if node is None:
        continue
    vals = [v for v in (e.get("value") for e in node.iter(XS + "enumeration")) if v]
    if vals:
        enums[name] = vals

doc = {
    "source": "QuickBooks Desktop SDK 16.0 qbXML schema (generated by scripts/gen-schema.py)",
    "roots": roots,
    "models": models,
    "enums": enums,
    "iterators": iterators,
    "counts": counts,
}
out.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True))
print(f"{len(roots)} messages, {len(models)} models, {len(enums)} enums, {len(iterators)} with iterators, {len(counts)} that count -> {out} ({out.stat().st_size // 1024} KB)")
