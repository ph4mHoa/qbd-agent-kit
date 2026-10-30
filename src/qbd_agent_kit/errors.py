"""Errors from QuickBooks and from the kit, each with the one thing a merchant can do about it."""
from __future__ import annotations

from .jsutil import num_str

# What to do for the connection errors people actually hit. QuickBooks' own message is always shown too.
HINTS = {
    "0x80040154": "The QuickBooks SDK is not registered for this program. QuickBooks Desktop must be installed on this PC; "
    "if it is, run `qbd doctor` to see what is missing.",
    "0x800401F3": "The QuickBooks SDK is not installed on this PC. Run qbd on the PC where QuickBooks Desktop is installed.",
    "0x80040401": "Open QuickBooks Desktop on this PC, open the company file, then try again.",
    "0x80040403": "Check the company file path in QBD_COMPANY_FILE, or leave it empty to use the file open in QuickBooks.",
    "0x80040408": "QuickBooks could not be started. Open QuickBooks Desktop and the company file yourself, then try again.",
    "0x80040410": "The company file is open in a different mode than requested. Switch QuickBooks to multi-user mode "
    "(File > Switch to Multi-user Mode) or close other apps connected to it.",
    "0x80040414": "A window or message box is open in QuickBooks. Close it (look for a dialog behind other windows), then try again. "
    "If it is the 'Application permission' dialog, answer it as described in docs/setup.md.",
    "0x80040416": "No company file is open. Open the company file in QuickBooks, or set QBD_COMPANY_FILE to its full path.",
    "0x80040418": "This app has not been allowed into this company file yet. Log into QuickBooks as the Admin user in single-user mode "
    "and run `qbd authorize`.",
    "0x8004041A": "This app is not allowed into this company file. In QuickBooks: Edit > Preferences > Integrated Applications > "
    "Company Preferences, select the app and allow access.",
    "0x8004041D": "This app is not allowed to log in automatically. In QuickBooks: Edit > Preferences > Integrated Applications > "
    "Company Preferences > Properties, tick 'Allow this application to login automatically'.",
    "0x80040420": "Access was refused in QuickBooks. Run `qbd authorize` and choose a 'Yes' option in the dialog.",
    "0x80040422": "Another app holds the company file in single-user mode. Close it or switch QuickBooks to multi-user mode.",
    "0x80040424": "QuickBooks is still starting up. Wait until the company file is fully open, then try again.",
}

# Connection errors only a person at the PC can clear: a dialog, QuickBooks closed, a permission.
NEEDS_PERSON = {
    "0x80040401", "0x80040403", "0x80040408", "0x80040410", "0x80040414", "0x80040416", "0x80040418",
    "0x8004041A", "0x8004041D", "0x80040420", "0x80040422", "0x80040424", "no-session",
}

STATUS_HINTS = {
    1030: "This edition or country version of QuickBooks does not support this request; some exist only in the US edition.",
    3100: "A record with this name already exists. Search for it and update it instead of adding a new one.",
    3120: "The record was not found. Search first and use the exact ListID, TxnID or full name it returns.",
    3140: "A referenced customer, vendor, item or account does not exist. Search for the exact name first.",
    3180: "QuickBooks could not save the record. Check that the record is not open in QuickBooks and the values are valid.",
    3200: "The record changed after it was read. Read it again and redo the change.",
    3220: "The feature is turned off in this company file, or the QuickBooks user this app logs in as may not use it. QuickBooks' message says which.",
    3250: "This feature is turned off in this company file or not available in this edition of QuickBooks.",
    3260: "The QuickBooks user this app logs in as is not allowed to do this. Change it in Integrated Applications preferences.",
}


def _hresult(code: str) -> str:
    return "0x" + code[2:].upper() if code[:2].lower() == "0x" else code


class QbConnectionError(Exception):
    """The QuickBooks SDK connection failed; code is the HRESULT such as 0x80040416, or a word such as timeout."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = _hresult(code)
        self.message = message
        self.hint = hint if hint is not None else HINTS.get(self.code)

    @property
    def needs_person(self) -> bool:
        return self.code in NEEDS_PERSON

    def __str__(self) -> str:
        return f"{self.message}{chr(10) + 'What to do: ' + self.hint if self.hint else ''} ({self.code})"


class QbTimeoutError(QbConnectionError):
    """QuickBooks did not answer in time. The request may still be running inside QuickBooks."""

    def __init__(self, seconds: float, message: str | None = None):
        super().__init__(
            "timeout",
            message or f"QuickBooks did not answer within {num_str(seconds)} s. The request may still be running inside QuickBooks.",
            "Wait until QuickBooks is responsive again, then retry with a smaller date range or limit. If QuickBooks stays "
            "frozen, it may have stopped working: close it (with Task Manager if needed) and open the company file again.",
        )


class QbNoSessionError(QbConnectionError):
    """QuickBooks did not open a session in time, most likely because a dialog is waiting for a person."""

    def __init__(self, seconds: float):
        super().__init__(
            "no-session",
            f"QuickBooks did not open a session within {num_str(seconds)} s. It is most likely waiting for someone at this "
            "PC to answer a dialog, such as 'Application permission' the first time qbd connects.",
            "Find the QuickBooks dialog on this PC; it may be behind other windows. For 'Application permission' choose "
            "'Yes, whenever my QuickBooks company file is open' and click Continue, then run the command again. "
            "`qbd authorize` waits 10 minutes for the answer.",
        )


class QbStatusError(Exception):
    """A qbXML status error, e.g. 3120 "object not found"."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.hint = STATUS_HINTS.get(status_code)

    def __str__(self) -> str:
        return f"QuickBooks status {self.status_code}: {self.message}{chr(10) + 'What to do: ' + self.hint if self.hint else ''}"


class QbxmlInputError(Exception):
    """A request that does not fit the qbXML schema."""


class QbxmlParseError(Exception):
    """QuickBooks sent something that is not readable qbXML."""


class ToolError(Exception):
    """A request the kit refuses, with a message that says what to change."""


class WriteUnconfirmed(Exception):
    """A change was sent but QuickBooks did not confirm it, so it may or may not be saved."""


class Stopped(BaseException):
    """A person asked in the Activity Monitor to stop this command after its current step. A BaseException, so the
    workflows that carry on past a failed step stop too."""

    def __init__(self, by: str, steps: int):
        super().__init__(f"Stopped by {by} in the Activity Monitor after {steps} step{'' if steps == 1 else 's'}. "
                         "Nothing was saved.")
        self.by = by
        self.steps = steps


class Paused(Exception):
    """A person paused this assistant's session in the Activity Monitor: its commands wait until they click Resume."""

    def __init__(self, by: str, at: str):
        super().__init__(f"{by} paused this session in the Activity Monitor ({at}), so qbd runs none of its commands until they "
                         "click Resume.")
        self.hint = "Tell the person; ask them to click Resume in the Activity Monitor's Live view, then run the command again."


class InvalidArguments(Exception):
    """Arguments that do not fit a command; issues are (path, message) pairs."""

    def __init__(self, issues: list[tuple[str, str]]):
        self.issues = issues
        super().__init__("; ".join(f"{p or 'input'}: {m}" for p, m in issues))
