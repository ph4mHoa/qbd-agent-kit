"""Personal data never reaches the model in full: ID numbers and card numbers keep their last four
characters, payroll and demographic fields are dropped."""
from __future__ import annotations

import re
from typing import Any

from ..jsutil import is_num, text

LAST_FOUR = {"SSN", "CreditCardNumber", "VendorTaxIdent", "RoutingNumber", "BankAccountNumber"}

DROP = {
    "EmployeePayrollInfo",
    "EmployeeDefaultPayrollInfo",
    "BirthDate",
    "Gender",
    "Ethnicity",
    "Disabled",
    "DisabilityDesc",
    "MilitaryStatus",
    "USCitizen",
    "USVeteran",
    "ExpirationMonth",
    "ExpirationYear",
    "NameOnCard",
    "CardSecurityCodeMatch",
    "ClosingDatePassword",
}


def _last_four(v: Any) -> str:
    s = re.sub(r"\s", "", text(v))
    return "****" if len(s) <= 4 else f"***{s[-4:]}"


def mask_personal_data(value: Any) -> Any:
    if isinstance(value, list):
        return [mask_personal_data(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k in DROP:
            continue
        out[k] = _last_four(v) if k in LAST_FOUR and (isinstance(v, str) or is_num(v)) else mask_personal_data(v)
    return out


def mask_xml(xml: str) -> str:
    """A qbXML request with the same data masked, for the write log."""
    xml = re.sub(r"<(%s)>([^<]*)</\1>" % "|".join(LAST_FOUR), lambda m: f"<{m[1]}>{_last_four(m[2])}</{m[1]}>", xml)
    return re.sub(r"<(%s)>.*?</\1>" % "|".join(DROP), lambda m: f"<{m[1]}/>", xml, flags=re.S)
