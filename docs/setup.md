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
- [6. See what the agent did](#6-see-what-the-agent-did)
- [7. Watch and approve in the Activity Monitor](#7-watch-and-approve-in-the-activity-monitor)
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
folder, for QuickBooks 2022 and later (64-bit), and `add-ons.cmd` adds the
[Activity Monitor](#7-watch-and-approve-in-the-activity-monitor). Unzip it to a short path without spaces, such as
`C:\qbd`, and run `C:\qbd\qbd.cmd` wherever this guide says `qbd` (for example `C:\qbd\qbd.cmd doctor`). To update,
unzip a newer download over it.

## 2. Try it without QuickBooks

A built-in sample company, *Bayside Hardware & Supply*, has realistic data and planted problems: overdue invoices,
duplicate bills, payments waiting to be deposited, late purchase orders and more. It works on any computer with
Python, Windows or not:

```
qbd daily-brief --demo --pretty
qbd help
```

`--pretty` indents the JSON so it is easier to read. In demo mode changes are allowed, and the sample company starts
fresh with every command, so nothing you try there is kept. Its write log and call log are kept apart, in `audit-demo.jsonl` and `calls-demo.jsonl`.

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
- the app name QuickBooks shows, whether changes are on, and where the write log and the call log are;
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

## 6. See what the agent did

`qbd activity --open` opens a page in the web browser: every request the agent made, how long it took and how many
records came back, what went wrong, and every change saved or tried with the reason given. With the Windows
download, double-click `what-the-ai-did.cmd` in its folder. The agent opens the page itself when you ask what it
did. The page is made from the [call log and the write log](safety.md#call-log-and-the-activity-page) each time it
opens.

## 7. Watch and approve in the Activity Monitor

The Activity Monitor is a small Windows app for the person who checks the AI's work. It sits in the tray and shows
what each assistant is doing in QuickBooks right now, what waits for an answer, what went wrong and what changed,
in plain words. It reads only what `qbd` writes on this PC (the logs, the drafts and the commands running now), never
connects to QuickBooks itself, and sends nothing anywhere.

It is an [add-on](addons.md), installed when someone wants it. With the Windows download, double-click
`add-ons.cmd`, type the Activity Monitor's number, then `y` to install it and `y` to start it. Or run:

```
qbd addons install activity-monitor --yes
qbd addons run activity-monitor
```

It downloads once from the kit's GitHub release, checked against the SHA-256 in qbd's catalog; on a PC without
internet, [install it from its zip](addons.md#on-a-pc-without-internet). It is then in the Start menu under
**QBD Agent Kit**, and **Start with Windows** in the tray icon's menu starts it at sign-in, in the tray. To try it on
a sample morning instead of this PC's logs, run
`C:\qbd\python\python.exe -m qbd_agent_kit.addons activity-monitor --sample`; nothing can be saved from the sample.

The monitor is built for the Windows download's Python (64-bit Python 3.15) and runs on 64-bit Windows 10 and
Windows 11. The 32-bit download, and `qbd` installed with another Python, have no monitor yet.

What a person can do there:

- **Approve and save**, or **Reject**, a change an assistant prepared. Approving runs `qbd commit` with this PC's
  settings, so every check of a commit applies, and saves only if the draft is still exactly what the screen showed.
- **Undo** a saved change: the reverse change is prepared and waits under Approvals like any other.
- **Stop after this step**: the running command ends after its current QuickBooks request, and that assistant's
  next commands wait until someone clicks **Resume**. A change is never stopped halfway.

So that each assistant shows under its own name, its skill tells it to name itself on every command
(`--agent`, `--session`, `--session-title`), or you can set the name in the assistant's environment, for Claude Code
in the `env` section of its settings file:

| Setting | Example | What it does |
| --- | --- | --- |
| `QBD_AGENT` | `Claude (Claude Code)` | The assistant's name. Without one, the monitor names the program that ran `qbd`, such as `agy.exe` |
| `QBD_SESSION` | `cc-7f3c` | The conversation or run, so its requests show as one session. Without one, an assistant's requests less than 30 minutes apart are one session |
| `QBD_SESSION_TITLE` | `Deposit this week's cheques` | What the session is about, in a few words (80 characters at most, long numbers masked) |

Assistants that run as other Windows accounts, such as an automation running as a service, keep their files in their
own account's folder. To see them in the monitor, give them and the monitor one shared folder with `QBD_STATE_DIR`
(see [Where files are kept](#where-files-are-kept)).

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
| `QBD_CALL_LOG` | `%LOCALAPPDATA%\qbd-agent-kit\calls.jsonl` | The call log, every command run; `calls-demo.jsonl` with `--demo`; `off` turns it off |
| `QBD_MASK_PERSONAL_DATA` | `true` | Shows only the last four digits of ID, card and bank numbers, and leaves out payroll and personal details |
| `QBD_DEMO` | `false` | Uses the sample company, like `--demo` |
| `QBD_AGENT` | empty: the program that started `qbd` | The assistant's name in the call log and the Activity Monitor; `--agent` sets it for one command |
| `QBD_SESSION` | empty | The assistant's conversation or run, so its requests show as one session; `--session` for one command |
| `QBD_SESSION_TITLE` | empty | What the session is about, 80 characters at most; `--session-title` for one command |
| `QBD_STATE_DIR` | `%LOCALAPPDATA%\qbd-agent-kit` | A folder for drafts, logs and the line for QuickBooks that assistants running as other Windows accounts share; the Activity Monitor reads it as well as its own |
| `QBD_REQUIRE_MONITOR_APPROVAL` | `false` | `qbd commit` saves only changes a person approved in the Activity Monitor. Like the other settings, it prevents mistakes; it is not a lock |

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
  after 15 minutes. `<token>.answer.json` keeps a person's answer in the Activity Monitor for 2 days, so the assistant
  learns it; `<token>.expired.json` keeps the summary of a draft nobody answered for 7 days, for the monitor's list.
- `audit.jsonl`: the write log. Every change committed or attempted, with time, Windows user and PC, company file,
  action, preview, reason, outcome (`done`, `failed` or `unknown`) and the IDs QuickBooks returned. `qbd log` reads
  it. With `--demo`, `audit-demo.jsonl`.
- `calls.jsonl`: the call log. Every command run, with its arguments, how long it took, how many records QuickBooks
  sent back and what went wrong; counts, not values from the company file. Past 5 MB it moves to
  `calls.jsonl.<date-time>` and a new one starts; parts older than 90 days are deleted. With `--demo`,
  `calls-demo.jsonl`.
- `activity.html`: the page `qbd activity` makes from the two logs. With `--demo`, `activity-demo.html`.
- `quickbooks.lock`: lets one command at a time talk to QuickBooks.
- `live\`: a small file for each command waiting for its turn or talking to QuickBooks, removed when it ends: who
  runs it, the step, the page and the records read, never values from QuickBooks. `<pid>.stop` asks that command to
  stop after its current step.
- `paused\`: the assistants whose next commands wait until someone clicks Resume in the Activity Monitor.
- `quickbooks.json`: the last facts read about each company file (product, version, country, closing date), for the
  monitor's Safety page. With `--demo`, `quickbooks-demo.json`.
- `monitor.json`: the Activity Monitor's own state: the assistants in the order first seen (their colours), when the
  problems were last looked at, and each log's size when last read.
- `addons\`: the [add-ons](addons.md) installed for this Windows user, also when `QBD_STATE_DIR` moves the other
  files (on other systems, in `~/.local/share/qbd-agent-kit/addons`).

`QBD_STATE_DIR` puts all of these in another folder, such as one under `C:\ProgramData` that every Windows account
on the PC can write to. Each account's own folder stays as it was; the monitor reads both.

## Where your data goes

`qbd` runs on this PC. It connects to the internet only to download an add-on someone installs from its catalog,
from the kit's GitHub releases, and it sends nothing to the authors or anyone else.

When an agent runs a `qbd` command, the agent's app reads the JSON and sends it to its AI model to answer you.
Before connecting real books, check how your AI provider stores and uses what you send.
