"""Settings come from environment variables, so any AI app or script that starts `qbd` can set them.
Everything is safe by default: read-only."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Mapping

from .jsutil import to_number

DEFAULT_WRITE_ENTITIES = [
    "customer",
    "vendor",
    "invoice",
    "estimate",
    "sales_order",
    "sales_receipt",
    "credit_memo",
    "receive_payment",
    "bill",
    "purchase_order",
]


@dataclass
class Config:
    app_name: str = "QBD Agent Kit"     # the name QuickBooks shows in its permission dialog
    app_id: str = ""
    company_file: str = ""              # "" = whichever file is open in QuickBooks
    qbxml_version: str | None = None    # force a qbXML version instead of asking QuickBooks
    allow_writes: bool = False          # draft/commit exist only when true
    allow_delete: bool = False          # delete and void need this as well
    write_entities: list[str] = field(default_factory=lambda: list(DEFAULT_WRITE_ENTITIES))
    max_amount: float = 10_000          # a single draft above this total is refused
    timeout_seconds: float = 120
    open_timeout_seconds: float = 120
    audit_log: str = ""
    mask_personal_data: bool = True
    demo: bool = False
    state_dir: str = ""


def _clean(env: Mapping[str, str]) -> dict[str, str]:
    """Empty, or a placeholder an app left unfilled (e.g. "${user_config.x}"), counts as not set."""
    return {k: v for k, v in env.items() if not k.startswith("QBD_") or (v is not None and not re.fullmatch(r"\$\{.*\}", v.strip(), re.S))}


def _flag(v: str | None, fallback: bool) -> bool:
    if v is None or v.strip() == "":
        return fallback
    return re.fullmatch(r"1|true|yes|on", v.strip(), re.I) is not None


def _num(v: str | None, fallback: float) -> float:
    n = to_number(v) if v is not None and v.strip() != "" else math.nan
    if not math.isfinite(n) or n <= 0:
        return fallback
    return int(n) if n.is_integer() else n


def state_dir(env: Mapping[str, str]) -> str:
    """Where drafts, the audit log and the lock live: %LOCALAPPDATA%\\qbd-agent-kit on Windows."""
    if env.get("LOCALAPPDATA"):
        return os.path.join(env["LOCALAPPDATA"], "qbd-agent-kit")
    base = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "qbd-agent-kit")


def load_config(raw_env: Mapping[str, str] | None = None, **overrides: object) -> Config:
    env = _clean(os.environ if raw_env is None else raw_env)
    demo = bool(overrides["demo"]) if "demo" in overrides else _flag(env.get("QBD_DEMO"), False)
    entities = [s.lower() for s in re.split(r"[\s,]+", env.get("QBD_WRITE_ENTITIES", "")) if s]
    sdir = state_dir(env)
    config = Config(
        app_name=(env.get("QBD_APP_NAME") or "").strip() or "QBD Agent Kit",
        app_id=(env.get("QBD_APP_ID") or "").strip(),
        company_file=(env.get("QBD_COMPANY_FILE") or "").strip(),
        qbxml_version=(env.get("QBD_QBXML_VERSION") or "").strip() or None,
        # The demo company lives in memory, so trying changes there is harmless.
        allow_writes=_flag(env.get("QBD_ALLOW_WRITES"), demo),
        allow_delete=_flag(env.get("QBD_ALLOW_DELETE"), demo),
        write_entities=list(dict.fromkeys(entities)) if entities else list(DEFAULT_WRITE_ENTITIES),
        max_amount=_num(env.get("QBD_MAX_AMOUNT"), 10_000),
        timeout_seconds=_num(env.get("QBD_TIMEOUT_SECONDS"), 120),
        open_timeout_seconds=_num(env.get("QBD_OPEN_TIMEOUT_SECONDS"), 120),
        audit_log=(env.get("QBD_AUDIT_LOG") or "").strip() or os.path.join(sdir, "audit-demo.jsonl" if demo else "audit.jsonl"),
        mask_personal_data=_flag(env.get("QBD_MASK_PERSONAL_DATA"), True),
        demo=demo,
        state_dir=sdir,
    )
    rest = {k: v for k, v in overrides.items() if k != "demo"}
    return replace(config, **rest) if rest else config
