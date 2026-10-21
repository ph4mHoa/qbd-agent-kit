# Troubleshooting

Every error `qbd` prints says what happened and, when it can, what to do. This page explains the error codes and the
problems people run into most.

- [Start with doctor](#start-with-doctor)
- [Exit codes and errors](#exit-codes-and-errors)
- [Connection errors](#connection-errors)
- [QuickBooks status errors](#quickbooks-status-errors)
- [Common problems](#common-problems)

## Start with doctor

```
qbd doctor
```

It checks Python, `pywin32`, where the QuickBooks SDK is registered, whether QuickBooks is running, the settings, and
then connects and reads a little. `OK` marks a check that passed, `!!` one that needs action. See
[setup](setup.md#4-check-the-connection) for the full list.

## Exit codes and errors

A failed command prints one line of JSON, for example:

```json
{"error": {"code": "0x80040414", "message": "<QuickBooks' own message>", "hint": "A window or message box is open in QuickBooks. Close it (look for a dialog behind other windows), then try again. ...", "retryable": false}}
```

| Exit code | Meaning | Error codes |
| --- | --- | --- |
| `0` | Done | |
| `2` | Change the command: `qbd` refused it before asking QuickBooks | `invalid_arguments` (run `qbd help <command>`), `invalid_request` (the fields do not fit qbXML; see `qbd schema`), `refused` (a safety check or setting, such as `QBD_MAX_AMOUNT` or changes turned off) |
| `3` | A person must act at the PC | the connection errors marked below, and `no-session` |
| `1` | Anything else | `timeout`, `busy`, `qb_status_<number>` (QuickBooks refused the request), `unconfirmed` (a change that may or may not be saved), the other connection errors, `internal` |

`retryable` is `true` only for `timeout` and `busy`, and never for `qbd commit`: a change that timed out may still
have been saved.

## Connection errors

QuickBooks' own message is always included; the hint says what to do. Errors that need a person at the PC exit with 3.

| Code | Meaning | What to do | Exit |
| --- | --- | --- | --- |
| `0x80040154` | The QuickBooks SDK is not registered for this program | QuickBooks Desktop must be installed on this PC; `qbd doctor` shows whether it is registered for 64-bit or 32-bit programs | 1 |
| `0x800401F3` | The QuickBooks SDK is not installed on this PC | Run `qbd` on the PC where QuickBooks Desktop is installed | 1 |
| `0x80040401` | QuickBooks could not be reached | Open QuickBooks Desktop and the company file, then try again | 3 |
| `0x80040403` | The company file could not be opened | Check the path in `QBD_COMPANY_FILE`, or leave it empty to use the file open in QuickBooks | 3 |
| `0x80040408` | QuickBooks could not be started | Open QuickBooks Desktop and the company file yourself, then try again | 3 |
| `0x80040410` | The file is open in a different mode than requested | Switch to multi-user mode (**File > Switch to Multi-user Mode**) or close other apps connected to it | 3 |
| `0x80040414` | A window or message box is open in QuickBooks | Close it; if it is **Application permission**, answer it ([below](#a-dialog-in-quickbooks-blocks-every-command)) | 3 |
| `0x80040416` | No company file is open | Open the company file, or set `QBD_COMPANY_FILE` to its full path | 3 |
| `0x80040418` | This app has not been allowed into this company file yet | Log in to QuickBooks as Admin, in single-user mode, and run `qbd authorize` | 3 |
| `0x8004041A` | This app is not allowed into this company file | **Edit > Preferences > Integrated Applications > Company Preferences**, select the app and allow access | 3 |
| `0x8004041D` | This app may not log in automatically | Same place, **Properties**: tick **Allow this application to login automatically** | 3 |
| `0x80040420` | Access was refused in QuickBooks | Run `qbd authorize` and choose a **Yes** option | 3 |
| `0x80040422` | Another app holds the file in single-user mode | Close it, or switch QuickBooks to multi-user mode | 3 |
| `0x80040424` | QuickBooks is still starting | Wait until the company file is fully open, then try again | 3 |
| `no-session` | QuickBooks did not open a session within `QBD_OPEN_TIMEOUT_SECONDS` (120) | A dialog is most likely waiting for an answer, such as **Application permission** the first time; `qbd authorize` waits 10 minutes | 3 |
| `timeout` | QuickBooks did not answer within `QBD_TIMEOUT_SECONDS` (120) | See [timeouts](#a-command-timed-out) | 1 |
| `busy` | Another `qbd` command is using QuickBooks, and this one waited up to 60 seconds for its turn | Run it again in a moment | 1 |
| `not-windows` | QuickBooks Desktop runs only on Windows | Run `qbd` on the QuickBooks PC, or add `--demo` | 1 |
| `no-pywin32` | `pywin32` is missing | `py -m pip install pywin32` | 1 |

## QuickBooks status errors

When QuickBooks refuses a request, the code is `qb_status_` and QuickBooks' number. The common ones:

| Number | Meaning | What to do |
| --- | --- | --- |
| `1030` | This edition or country version does not support the request | Some requests exist only in the US edition; a report may give the same answer |
| `3100` | A record with this name already exists | Search for it and update it instead |
| `3120` | The record was not found | Search first and use the exact ListID, TxnID or full name it returns |
| `3140` | A referenced customer, vendor, item or account does not exist | Search for the exact name first |
| `3180` | QuickBooks could not save the record | Check that the record is not open in QuickBooks and the values are valid |
| `3200` | The record changed after it was read | Read it again and redo the change |
| `3220` | The feature is off, or the QuickBooks user the app works as may not use it | QuickBooks' message says which |
| `3250` | The feature is off in this file or not in this edition | Turn it on in QuickBooks' preferences, if your edition has it |
| `3260` | The QuickBooks user the app logs in as may not do this | Change the user's rights, or the user, in Integrated Applications preferences |

## Common problems

### QuickBooks is closed, or no file is open

`0x80040401` or `0x80040416`, exit code 3. Start QuickBooks and open the company file. For `qbd` to work while
QuickBooks is closed, give the app "always allow" access when you authorize it, and set `QBD_COMPANY_FILE`.

### The permission window was never answered

`no-session` after two minutes, or `0x80040414`, the first time `qbd` connects. QuickBooks is showing **Application
permission**, often behind other windows:

1. Bring QuickBooks to the front from the taskbar.
2. Choose **Yes, whenever my QuickBooks company file is open**, leave personal data unticked, type `yes` if asked,
   then **Continue** and **Done**.
3. Next time, start with `qbd authorize`: it waits 10 minutes for the answer.

### A dialog in QuickBooks blocks every command

`0x80040414`, exit code 3. While QuickBooks shows a window that waits for an answer (an error message, a reminder, a
preferences window, the permission window, or "Thank you for submitting your report!" after QuickBooks crashed), it
answers no SDK requests. Find that window, on the taskbar or behind others, close it, and run the command again.

### Another company file is open than the one in QBD_COMPANY_FILE

QuickBooks has one company file open at a time, and refuses the session for a different one. Open the file named in
`QBD_COMPANY_FILE` in QuickBooks, or clear the setting so `qbd` uses whichever file is open.

### A command timed out

A request already sent to QuickBooks cannot be cancelled. When a command times out, QuickBooks keeps working on it,
and `qbd` exits without waiting. The next command waits for QuickBooks to finish that work first, so it can time out
too.

- Wait until QuickBooks responds in its own window again, then run the command with a narrower date range or a
  smaller limit.
- If QuickBooks stays frozen, close it (with Task Manager if needed) and open the company file again.
- A `qbd commit` that does not get an answer ends with `unconfirmed` and is logged as `unknown`: the change may or
  may not be saved. Check `qbd log`, and search for the record (`qbd search <type>`), before trying again.

### Commands are slow on a big company file

QuickBooks works through one request at a time, and large files take longer. Narrow the request:

- give a period with `--from` and `--to`;
- name the customer, vendor, account or item;
- keep `--limit` small (searches return 25 records unless told otherwise);
- run detail reports (general ledger, journal) for a month or a quarter, not a year.

### A file written with > in PowerShell cannot be read

Windows PowerShell 5.1, the one built into Windows 10 and 11, writes UTF-16 when you use `>`, which many programs
cannot read as JSON. Use `--out` instead, which writes UTF-8:

```
qbd daily-brief --out brief.json
```

### CSV import says the file is not UTF-8

`The file is not UTF-8. In Excel, save it as "CSV UTF-8".` Excel's plain **CSV (Comma delimited)** uses an older
Windows encoding. In Excel choose **File > Save As**, pick **CSV UTF-8 (Comma delimited)**, and import that file.
Files written by `qbd export` are already UTF-8.

### The SDK is registered for 32-bit programs only

`0x80040154`, or `qbd doctor` says: *This Python is 64-bit, but the QuickBooks SDK is registered for 32-bit programs
only.* A program can only use the SDK registered for its own bitness. Install the 32-bit Python from python.org
(**Windows installer (32-bit)**), install `qbd` with it, and run `qbd` from that Python.

### QuickBooks asks for permission again after changing Python

QuickBooks recognises the app by its name and by the publisher of the program that connects. A newer Python from
python.org keeps the permission; a Python from another publisher, or a new `QBD_APP_NAME`, counts as a new app. Log
in as Admin with the company file open and run `qbd authorize` again. Old entries can be removed in
**Edit > Preferences > Integrated Applications > Company Preferences**.
