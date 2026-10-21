"""A small, forgiving XML reader for qbXML. QuickBooks writes plain XML: elements, attributes,
text and numeric character references. Real company files hold odd characters that strict
parsers reject, so this reader never refuses a document; it reads what is there.

Two views of a document:
  read(xml)       a tree of Element (tag, attrs, kids), kids being Elements and text strings in order
  compact(el)     the children of an element as a dict: repeated tags become lists, attributes are "@name"
                  keys, text is "#text", a text-only element becomes its string and an empty one "".
"""
from __future__ import annotations

import re

_ENTITY = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6})|(amp|lt|gt|quot|apos));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_ATTR = re.compile(r"""([^\s=]+)\s*(=\s*(['"])(.*?)\3)?""", re.S)
_TOKEN = re.compile(
    r"""<!--.*?-->"""
    r"""|<!\[CDATA\[(?P<cdata>.*?)\]\]>"""
    r"""|<\?.*?\?>"""
    r"""|<!(?:[^>"'\[]|"[^"]*"|'[^']*'|\[[^\]]*\])*>"""
    r"""|</[^>]*>"""
    r"""|<(?P<tag>[^\s/>!?]+)(?P<rest>(?:[^>"']|"[^"]*"|'[^']*')*)>"""
    r"""|(?P<text>[^<]+|<)""",
    re.S,
)


def _char(code: int) -> str:
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�"


def _entity(m: re.Match) -> str:
    if m.group(1):
        return _char(int(m.group(1)))
    if m.group(2):
        return _char(int(m.group(2), 16))
    return _NAMED[m.group(3)]


def decode(text: str) -> str:
    """The five XML entities and numeric references, in one pass: "&amp;#1;" stays "&#1;"."""
    return _ENTITY.sub(_entity, text) if "&" in text else text


class Element:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.kids: list[Element | str] = []

    def find(self, tag: str) -> Element | None:
        for k in self.kids:
            if isinstance(k, Element) and k.tag == tag:
                return k
        return None

    def elements(self) -> list[Element]:
        return [k for k in self.kids if isinstance(k, Element)]

    def text(self) -> str:
        """The first text inside, as QuickBooks puts a value in one piece."""
        for k in self.kids:
            if isinstance(k, str):
                return k
        return ""


def _attrs(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR.finditer(s):
        if m.group(2) is not None:
            out[m.group(1)] = decode(m.group(4).strip())
    return out


def read(xml: str) -> Element:
    """The document as a tree under a nameless root. Declarations, comments and doctypes are skipped,
    text is trimmed and empty text dropped, closing tags close the innermost open element."""
    xml = xml.replace("\r\n", "\n").replace("\r", "\n")
    root = Element("", {})
    stack = [root]
    text = ""

    def flush() -> None:
        nonlocal text
        t = text.strip()
        if t and len(stack) > 1:
            stack[-1].kids.append(decode(t))
        text = ""

    for m in _TOKEN.finditer(xml):
        if m.group("text") is not None:
            text += m.group("text")
        elif m.group("tag") is not None:
            flush()
            rest = m.group("rest")
            closing = rest.endswith("/")
            el = Element(m.group("tag"), _attrs(rest[:-1] if closing else rest))
            stack[-1].kids.append(el)
            if not closing:
                stack.append(el)
        elif m.group("cdata") is not None:
            flush()
            if len(stack) > 1 and m.group("cdata"):
                stack[-1].kids.append(m.group("cdata"))
        elif m.group(0).startswith("</"):
            flush()
            if len(stack) > 1:
                stack.pop()
        elif m.group(0).startswith("<?"):
            flush()
        # comments and doctypes leave the text around them as one piece
    return root


def compact(el: Element) -> dict:
    """The children of el as a dict, see the module notes."""
    out: dict = {}
    text: str | None = None
    for k in el.kids:
        if isinstance(k, str):
            text = k if text is None else text + k
            continue
        val: object = compact(k)
        if k.attrs:
            for a, v in k.attrs.items():
                val["@" + a] = v  # type: ignore[index]
        elif len(val) == 1 and "#text" in val:  # type: ignore[arg-type]
            val = val["#text"]  # type: ignore[index]
        elif not val:
            val = ""
        if k.tag in out:
            cur = out[k.tag]
            if isinstance(cur, list):
                cur.append(val)
            else:
                out[k.tag] = [cur, val]
        else:
            out[k.tag] = val
    if text:
        out["#text"] = text
    return out


def parse(xml: str) -> dict:
    """The whole document in the compact form, e.g. {"QBXML": {"QBXMLMsgsRs": {...}}}."""
    return compact(read(xml))
