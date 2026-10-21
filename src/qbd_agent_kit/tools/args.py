"""Tool arguments. Each tool declares its arguments once; the declaration checks a call, fills in defaults
and gives the help text. A wrong call gets every problem at once, e.g.
"limit: Too small: expected number to be >=1; from: use a date like 2026-10-31"."""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidArguments
from ..jsutil import MISSING, is_num
from ..qb.dates import is_date


@dataclass(frozen=True)
class Arg:
    kind: str                       # str, date, enum, int, bool, list (of text or of choices), dict
    help: str = ""
    default: Any = MISSING          # used when the argument is left out
    required: bool = False
    choices: tuple[str, ...] = ()   # enum: the allowed values; list: allowed items (none = any text)
    min: int | None = None
    max: int | None = None


def _received(v: Any) -> str:
    if v is MISSING:
        return "undefined"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if is_num(v):
        return "NaN" if v != v else "Infinity" if math.isinf(v) else "number"
    return {str: "string", list: "array", dict: "object"}.get(type(v), type(v).__name__)


def _expected(what: str, v: Any) -> str:
    return f"Invalid input: expected {what}, received {_received(v)}"


def _check(arg: Arg, v: Any, path: str, issues: list[tuple[str, str]]) -> Any:
    kind = arg.kind
    if kind == "enum":
        if not isinstance(v, str) or v not in arg.choices:
            issues.append((path, "Invalid option: expected one of " + "|".join(f'"{c}"' for c in arg.choices)))
    elif kind in ("str", "date"):
        if not isinstance(v, str):
            issues.append((path, _expected("string", v)))
        elif kind == "date" and not is_date(v):
            issues.append((path, "use a date like 2026-10-31"))
    elif kind == "int":
        if not is_num(v) or not math.isfinite(v):
            issues.append((path, _expected("number", v)))
        elif not isinstance(v, int) and not v.is_integer():
            issues.append((path, "Invalid input: expected int, received number"))
        else:
            v = int(v)
            if arg.min is not None and v < arg.min:
                issues.append((path, f"Too small: expected number to be >={arg.min}"))
            if arg.max is not None and v > arg.max:
                issues.append((path, f"Too big: expected number to be <={arg.max}"))
    elif kind == "bool":
        if not isinstance(v, bool):
            issues.append((path, _expected("boolean", v)))
    elif kind == "list":
        if not isinstance(v, list):
            issues.append((path, _expected("array", v)))
        else:
            item = Arg("enum", choices=arg.choices) if arg.choices else Arg("str")
            v = [_check(item, x, f"{path}.{i}", issues) for i, x in enumerate(v)]
    elif kind == "dict":
        if not isinstance(v, dict):
            issues.append((path, _expected("record", v)))
    return v


def parse_args(spec: dict[str, Arg], args: Any) -> dict[str, Any]:
    """The checked arguments, with defaults filled in and MISSING for optional ones left out. Unknown keys are dropped."""
    if args is None or args is MISSING:
        args = {}
    if not isinstance(args, dict):
        raise InvalidArguments([("", _expected("object", args))])
    issues: list[tuple[str, str]] = []
    out: dict[str, Any] = {}
    for name, arg in spec.items():
        v = args.get(name, MISSING)
        if v is MISSING and not arg.required:
            out[name] = copy.deepcopy(arg.default)
        else:
            out[name] = _check(arg, v, name, issues)
    if issues:
        raise InvalidArguments(issues)
    return out
