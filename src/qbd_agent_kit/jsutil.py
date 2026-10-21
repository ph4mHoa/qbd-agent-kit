"""Numbers, text and JSON written the same way every time: amounts as 1,234.50, no "48.0" for
whole numbers, JSON keys in the order they were set.

MISSING stands for "no value at all": dictionary entries holding it are left out of the JSON,
while None is written as null.
"""
from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable


class _Missing:
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"

    def __reduce__(self) -> str:
        return "MISSING"  # copies stay the one MISSING


MISSING: Any = _Missing()


def is_num(v: Any) -> bool:
    """A real number (bool is not one, unlike in Python's own idea of int)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def truthy(v: Any) -> bool:
    """Whether a value counts as set: empty text, 0, NaN, None and MISSING do not; any dict or list does."""
    if v is None or v is MISSING or v is False:
        return False
    if isinstance(v, float):
        return v == v and v != 0
    if isinstance(v, (int, str)):
        return bool(v)
    return True


def nz(*values: Any) -> Any:
    """The first value that is neither None nor MISSING; the last one otherwise."""
    for v in values[:-1]:
        if v is not None and v is not MISSING:
            return v
    return values[-1]


def get(obj: Any, key: str) -> Any:
    """obj[key] for a dict, MISSING for anything else or an absent key."""
    return obj.get(key, MISSING) if isinstance(obj, dict) else MISSING


def add_up(values: Iterable[float]) -> float:
    """Adds from left to right. sum() may compensate rounding on newer Pythons, which changes the last digit."""
    total: float = 0
    for v in values:
        total = total + v
    return total


def js_round(x: float) -> float:
    """Rounds halves up, towards +infinity: 2.5 -> 3, -2.5 -> -2."""
    if not math.isfinite(x):
        return x
    f = math.floor(x)
    return float(f + 1 if x - f >= 0.5 else f)


def round2(x: float) -> float:
    return js_round(x * 100) / 100


def _digits(x: float) -> tuple[str, int]:
    """Shortest digits of x > 0 and the position of the decimal point: x = 0.DIGITS * 10**n."""
    mant, _, exp = repr(x).partition("e")
    whole, _, frac = mant.partition(".")
    digits = whole + frac
    n = len(whole) + (int(exp) if exp else 0)
    stripped = digits.lstrip("0")
    n -= len(digits) - len(stripped)
    return stripped.rstrip("0") or "0", n


def num_str(x: Any) -> str:
    """A number as text: 48 not 48.0, 0.1 not 0.1000000000000000055, 1e-7 for tiny values."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    if x < 0:
        return "-" + num_str(-x)
    digits, n = _digits(x)
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    e = n - 1
    mantissa = digits[0] + (f".{digits[1:]}" if k > 1 else "")
    return f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"


def to_fixed(x: float, places: int) -> str:
    """Rounds the exact stored value, halves away from zero, so 1.005 (stored as 1.00499...) gives 1.00."""
    if isinstance(x, float) and (math.isnan(x) or abs(x) >= 1e21):
        return num_str(x)
    if x < 0:
        return "-" + to_fixed(-x, places)
    q = Decimal(float(x) if x else 0.0).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(q, "f")


def money(x: float) -> str:
    """1234.5 -> '1,234.50'. Rounds the shortest decimal form, so 1.005 shows as 1.01."""
    if isinstance(x, float) and not math.isfinite(x):
        return num_str(x).replace("Infinity", "∞")
    q = Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(q, ",f")


_WS = " \t\n\v\f\r                 　﻿"
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)\Z")
_RADIX = re.compile(r"0([xXoObB])([0-9a-fA-F]+)\Z")


def to_number(s: str) -> float:
    """A strict reading of text as a number: '12.50' -> 12.5, '' -> 0, 'abc' or '1_000' -> NaN."""
    s = s.strip(_WS)
    if not s:
        return 0.0
    if _DECIMAL.match(s):
        return float(s)
    if s in ("Infinity", "+Infinity"):
        return math.inf
    if s == "-Infinity":
        return -math.inf
    m = _RADIX.match(s)
    if m:
        try:
            return float(int(m.group(2), {"x": 16, "o": 8, "b": 2}[m.group(1).lower()]))
        except ValueError:
            return math.nan
    return math.nan


def to_num(v: Any) -> float:
    """Any value as a number: text is read strictly, None counts as 0, anything unreadable is NaN."""
    if v is MISSING:
        return math.nan
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return to_number(v)
    if isinstance(v, list) and len(v) <= 1:
        return to_number(text(v[0])) if v else 0.0
    return math.nan


def finite(v: Any) -> bool:
    return is_num(v) and math.isfinite(v)


_INT_PREFIX = re.compile(r"[+-]?[0-9]+")


def parse_int(s: str) -> float:
    """The leading whole number of a text: '12abc' -> 12, '3.7' -> 3, 'x' -> NaN."""
    m = _INT_PREFIX.match(s.lstrip(_WS))
    return int(m.group(0)) if m else math.nan


def text(v: Any) -> str:
    """Any value as text, the way it would print in a message."""
    if v is None:
        return "null"
    if v is MISSING:
        return "undefined"
    if isinstance(v, str):
        return v
    if isinstance(v, (bool, int, float)):
        return num_str(v)
    if isinstance(v, (list, tuple)):
        return ",".join("" if x is None or x is MISSING else text(x) for x in v)
    return "[object Object]"


def _write(v: Any, out: list[str], indent: str | None, level: int) -> None:
    if v is None or v is MISSING:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, int):
        out.append(str(v))
    elif isinstance(v, float):
        out.append(num_str(v) if math.isfinite(v) else "null")
    elif isinstance(v, dict):
        items = [(k, x) for k, x in v.items() if x is not MISSING]
        if not items:
            out.append("{}")
            return
        inner = None if indent is None else "\n" + indent * (level + 1)
        out.append("{")
        for i, (k, x) in enumerate(items):
            if i:
                out.append(",")
            if inner:
                out.append(inner)
            out.append(json.dumps(str(k), ensure_ascii=False))
            out.append(":" if indent is None else ": ")
            _write(x, out, indent, level + 1)
        if indent is not None:
            out.append("\n" + indent * level)
        out.append("}")
    elif isinstance(v, (list, tuple)):
        if not v:
            out.append("[]")
            return
        inner = None if indent is None else "\n" + indent * (level + 1)
        out.append("[")
        for i, x in enumerate(v):
            if i:
                out.append(",")
            if inner:
                out.append(inner)
            _write(x, out, indent, level + 1)
        if indent is not None:
            out.append("\n" + indent * level)
        out.append("]")
    else:
        raise TypeError(f"cannot write {type(v).__name__} as JSON")


def dumps(v: Any, pretty: bool = False) -> str:
    """JSON with numbers as num_str writes them; entries holding MISSING are left out."""
    out: list[str] = []
    _write(v, out, "  " if pretty else None, 0)
    return "".join(out)
