# Setup

`qbd` runs on the Windows PC where QuickBooks Desktop is installed. It talks to QuickBooks through the QuickBooks
SDK that QuickBooks installs (the COM component `QBXMLRP2.RequestProcessor`, reached with `pywin32`), and every
command prints one line of JSON. It uses no Web Connector and no cloud service.

- [What you need](#what-you-need)
- [1. Install](#1-install)
- [2. Try it without QuickBooks](#2-try-it-without-quickbooks)
- [3. Give QuickBooks permission](#3-give-quickbooks-permission)
- [4. Check the connection](#4-check-the-connection)
- [5. Connect an AI agent](#5-connect-an-ai-agent)
- [Settings](#settings)
- [Allowing changes](#allowing-changes)
- [Where files are kept](#where-files-are-kept)
- [Where your data goes](#where-your-data-goes)

## What you need

- A Windows PC with QuickBooks Desktop (Pro, Premier, Accountant or Enterprise) installed. The company file can be
  on this PC or on a network share, but QuickBooks itself must be installed where `qbd` runs.
- Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/). In the installer, tick
  **Add python.exe to PATH**.
- An AI agent that can run commands on this PC, such as Claude Code, Codex or Cursor.

## 1. Install

Open Command Prompt or PowerShell and run:

```
py -m pip install https://github.com/ph4mHoa/qbd-agent-kit/archive/refs/heads/main.zip
```

This installs the `qbd` command and `pywin32`. Check it:

```
qbd --version
```

If Windows says `qbd` is not recognized, Python's `Scripts` folder is not on the PATH: run `py -m qbd_agent_kit`
instead of `qbd` (for example `py -m qbd_agent_kit doctor`), or reinstall Python with **Add python.exe to PATH**
ticked.

To update later, run the same `pip install` command with `--force-reinstall --no-deps` added.

Without Python: download `qbd-agent-kit-<version>-windows-x64.zip` from
[Releases](https://github.com/ph4mHoa/qbd-agent-kit/releases/latest). It holds Python, pywin32 and the kit in one
folder, for QuickBooks 2022 and later (64-bit). Unzip it to a short path without spaces, such as `C:\qbd`, and run
`C:\qbd\qbd.cmd` wherever this guide says `qbd` (for example `C:\qbd\qbd.cmd doctor`). To update, unzip a newer
download over it.

## 2. Try it without QuickBooks

A built-in sample company, *Bayside Hardware & Supply*, has realistic data and planted problems: overdue invoices,
duplicate bills, payments waiting to be deposited, late purchase orders and more. It works on any computer with
Python, Windows or not:

```
qbd daily-brief --demo --pretty
qbd help
```

`--pretty` indents the JSON so it is easier to read. In demo mode changes are allowed, and the sample company starts
fresh with every command, so nothing you try there is kept. Its write log is kept apart, in `audit-demo.jsonl`.

## 3. Give QuickBooks permission

QuickBooks asks the Admin to approve every program that connects to a company file. You do this once per company
file.

1. Open QuickBooks Desktop on this PC.
2. Open the company file and log in as the **Admin** user, in single-user mode if QuickBooks asks for it.
3. Run:

   ```
   qbd authorize
   ```

4. Switch to QuickBooks. An **Application permission** window asks whether to let **QBD Agent Kit** in. It often
   opens behind other windows; look on the taskbar.
5. Choose one of:
   - **Yes, whenever my QuickBooks company file is open** (recommended). `qbd` works while QuickBooks is open with
     this file.
   - **Yes, always allow access; even when my QuickBooks isn't running**, if the agent must work while QuickBooks is
     closed. Pick the QuickBooks user it logs in as: QuickBooks gives the app only that user's rights. Also set
     `QBD_COMPANY_FILE` (see [Settings](#settings)), because no file is open for it to use.
6. Leave **Allow this application to access personal data such as Social Security Number and customer credit card
   information** unticked. `qbd` does not need it; only `qbd 1099` would use it, to see whether vendors' tax IDs are
   on file, and it says so when they are not shared.
7. If QuickBooks asks you to confirm by typing `yes`, type it and click **Continue**, then **Done**.

`qbd authorize` waits up to 10 minutes for these steps. QuickBooks keeps the permission in the company file. To see,
change or remove it later: **Edit > Preferences > Integrated Applications > Company Preferences**.

QuickBooks recognises the app by its name and by the publisher of the program that connects. Updating to a newer
Python from python.org keeps the permission; a Python from another publisher counts as a new app and needs
`qbd authorize` again.

## 4. Check the connection

```
qbd doctor
```

`qbd doctor` checks, in order:

- the Python version and whether it is 64-bit or 32-bit;
- that `pywin32` is installed;
- where the QuickBooks SDK is registered (for 64-bit programs, 32-bit programs, or both), and whether that fits this
  Python;
- whether QuickBooks is running;
- the app name QuickBooks shows, whether changes are on, and where the write log is;
- the connection: the company name and file, the QuickBooks version and qbXML version, the closing date, and a small
  test read of the customer list.

`OK` marks a check that passed, `!!` one that needs action, and `-` a line for information. When everything passes,
it suggests a first command.

## 5. Connect an AI agent

Any agent that can run commands on this PC can use `qbd`.

**Claude Code.** Add the plugin, which brings the [skills](../skills) that teach Claude the commands and the
approval rules:

```
claude plugin marketplace add ph4mHoa/qbd-agent-kit
claude plugin install qbd-agent-kit@qbd-agent-kit
```

**Other agents.** Tell the agent to run `qbd help` first, and `qbd help <command>` before using a command. Every
command prints one line of JSON; errors are JSON too, with a hint and an exit code (see
[troubleshooting](troubleshooting.md#exit-codes-and-errors)). Agents that read an `AGENTS.md` file can get one line
there: "For QuickBooks, use the qbd command; run qbd help first."

## Settings

Settings are environment variables. All are optional and start safe.

| Setting | Default | What it does |
| --- | --- | --- |
| `QBD_ALLOW_WRITES` | `false` (`true` with `--demo`) | Turns on `draft`, `import`, `commit`, `undo` and `log` |
| `QBD_ALLOW_DELETE` | `false` (`true` with `--demo`) | Also allows drafting deletes and voids; needs `QBD_ALLOW_WRITES` |
| `QBD_WRITE_ENTITIES` | `customer, vendor, invoice, estimate, sales_order, sales_receipt, credit_memo, receive_payment, bill, purchase_order` | The record types `draft` and `import` may change, separated by commas; `qbd help draft` lists every type |
| `QBD_MAX_AMOUNT` | `10000` | A change whose total is above this is refused; the total counts the prices QuickBooks fills in |
| `QBD_COMPANY_FILE` | empty: the file open in QuickBooks | Full path of the `.qbw` file. Every command then opens only this file |
| `QBD_TIMEOUT_SECONDS` | `120` | How long to wait for QuickBooks to answer one request; also caps the wait for another command's turn, at most 60 seconds |
| `QBD_OPEN_TIMEOUT_SECONDS` | `120` | How long to wait for QuickBooks to open a session; a dialog waiting for an answer makes this run out |
| `QBD_APP_NAME` | `QBD Agent Kit` | The name QuickBooks shows in its permission window. A new name is a new app, which needs `qbd authorize` again |
| `QBD_APP_ID` | empty | An application ID for QuickBooks' connection call, if you have one |
| `QBD_QBXML_VERSION` | empty: the newest QuickBooks supports, up to 16.0 | Forces a qbXML version, such as `13.0` |
| `QBD_AUDIT_LOG` | `%LOCALAPPDATA%\qbd-agent-kit\audit.jsonl` | The write log; `audit-demo.jsonl` with `--demo` |
| `QBD_MASK_PERSONAL_DATA` | `true` | Shows only the last four digits of ID, card and bank numbers, and leaves out payroll and personal details |
| `QBD_DEMO` | `false` | Uses the sample company, like `--demo` |

To set one for every new window, use `setx` once, then open a new window:

```
setx QBD_COMPANY_FILE "C:\Users\Public\Documents\Intuit\QuickBooks\Company Files\MyCompany.qbw"
```

In Claude Code you can also put them in the `env` section of its settings file, for example
`"env": {"QBD_COMPANY_FILE": "C:\\...\\MyCompany.qbw"}`.

These settings prevent mistakes, but an agent that can run any command can also change them; see
[safety](safety.md#settings-are-not-a-lock) for what actually holds.

## Allowing changes

Out of the box `qbd` only reads. To let the agent draft changes that you approve:

```
setx QBD_ALLOW_WRITES true
```

To limit what it can touch, for example only bills and purchase orders up to 5,000:

```
setx QBD_WRITE_ENTITIES bill,purchase_order
setx QBD_MAX_AMOUNT 5000
```

`setx QBD_ALLOW_DELETE true` also allows deletes and voids, each still approved by you.

To make sure every change goes to the right company file when several are used on this PC, set `QBD_COMPANY_FILE`.
`qbd` then opens only that file, and QuickBooks refuses the session while another file is open. A draft's token also
commits only to the company file it was drafted on.

How a change is made:

1. **Draft.** The agent runs `qbd draft <action> <type>`, or `qbd import <type>` for a CSV file. `qbd` checks the
   change against the company file (closing date, names that exist, amount limit, duplicates) and returns a
   plain-language preview and a token of 8 characters, such as `a1b2c3d4`. Nothing is saved yet.
2. **Review.** The agent shows you the preview and any warnings.
3. **Commit.** Only after you say yes, the agent runs `qbd commit <token>`, which sends exactly the change you saw.

A token works once, for 15 minutes, and only with the company file it was drafted on. `qbd undo <token>` drafts the
reverse of a saved change. [Safety](safety.md) lists every check.

## Where files are kept

`qbd` keeps its files in `%LOCALAPPDATA%\qbd-agent-kit` (on other systems, `$XDG_STATE_HOME/qbd-agent-kit` or
`~/.local/state/qbd-agent-kit`):

- `drafts\`: changes waiting for approval, one file per token. A draft is removed when it is committed, and expires
  after 15 minutes.
- `audit.jsonl`: the write log. Every change committed or attempted, with time, Windows user and PC, company file,
  action, preview, reason, outcome (`done`, `failed` or `unknown`) and the IDs QuickBooks returned. `qbd log` reads
  it. With `--demo`, `audit-demo.jsonl`.
- `quickbooks.lock`: lets one command at a time talk to QuickBooks.

## Where your data goes

`qbd` runs on this PC and never connects to the internet. It sends nothing to the authors or anyone else.

When an agent runs a `qbd` command, the agent's app reads the JSON and sends it to its AI model to answer you.
Before connecting real books, check how your AI provider stores and uses what you send.
