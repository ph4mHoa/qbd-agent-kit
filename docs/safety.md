# Safety

This page is for owners, accountants and IT people deciding whether to let an AI agent work in a company file. It
describes what `qbd` enforces in code, and what it leaves to you, your AI app and QuickBooks.

## Read-only by default

Out of the box no command can change data. `draft`, `import`, `commit`, `undo` and `log` refuse to run until the
owner sets `QBD_ALLOW_WRITES=true`, and `qbd raw` accepts only query and report requests.

## Settings are not a lock

The settings are environment variables. They prevent mistakes: an agent asked a question does not change the books
by accident. They do not stop an agent that can run any command, because it can set `QBD_ALLOW_WRITES=true` in its
own command line. What does hold:

- **Your AI app's approval prompt.** Run the agent in a mode where it asks before commands you have not allowed in
  advance, and never allow `qbd commit` in advance. The token (below) makes sure that what is saved is exactly what
  was previewed, but it cannot prove that a person said yes: an agent could run `qbd commit` right after
  `qbd draft`. Your app's prompt is what stops that.
- **QuickBooks' own user rights.** In "Yes, always allow access" mode you choose the QuickBooks user the app logs in
  as, and QuickBooks gives the app only that user's rights. A user with view-only rights keeps the app read-only,
  whatever the agent runs.

## How a change is made

1. **Draft.** `qbd draft` (or `qbd import` for a CSV file) checks the change against the company file and returns a
   plain-language preview, any warnings, and a one-time token. Nothing is saved. The preview names what QuickBooks
   will fill in when it saves: item prices and costs, the open lines of a linked order, the payments a deposit takes
   in, the invoices a payment is applied to, with names, dates and amounts instead of IDs.
2. **Approve.** The agent shows you the preview and the warnings. You say yes or no, in the agent's chat or in the
   [Activity Monitor](#the-activity-monitor).
3. **Commit.** `qbd commit <token>` sends exactly the request that was previewed. `qbd` keeps the draft in its own
   folder, so the agent cannot change it between preview and commit. An approval in the Activity Monitor also names
   the draft's fingerprint as it was shown: if the draft's file changed after that, nothing is saved.
4. **Log.** The outcome goes to the write log, which `qbd log` reads and the `qbd activity` page shows.
5. **Undo.** `qbd undo <token>` drafts the reverse of a saved change, to approve like any other change.

### What a draft is checked for

| Check | Result |
| --- | --- |
| Record type not in `QBD_WRITE_ENTITIES` | refused |
| Delete or void without `QBD_ALLOW_DELETE=true` | refused |
| Date on or before the QuickBooks closing date (the new date, or the date of the record being changed, deleted or voided) | refused |
| Total above `QBD_MAX_AMOUNT` (new transactions, and updates that change lines), counting the prices QuickBooks fills in | refused |
| Text longer than QuickBooks takes in its field (an invoice number over 11 characters, a customer name over 41), from the SDK's schema | refused, naming the field and its limit |
| A customer, vendor, item, account, class, terms, payment method, ship method or tax code that does not exist | refused, with similar names suggested |
| A list record whose name already exists | refused; use update or upsert |
| The same customer or vendor already has a record with this number within 30 days | refused, unless `--allow-duplicate` is set |
| The change's source (`--source`: a file name, an email's Message-ID, a CSV row) was already saved to this company file | refused, unless `--allow-duplicate` is set |
| An update that would drop lines (QuickBooks deletes every line left out of a change) | refused, unless the lines are named in `--remove-lines` |
| The same party and amount within 3 days | warning |
| A date more than a month ahead | warning |
| A journal entry whose debits and credits differ | warning |
| A payment applied for more than an invoice has open, or for more than the payment | warning |
| Deleting a name that has a balance | warning |

### What the commit checks

- The token exists, has not been used and is less than 15 minutes old. Each token works once.
- QuickBooks still has the same company file open as when the change was drafted.
- The change's source has not been saved by another draft in the meantime.
- For updates, QuickBooks itself refuses the change if the record was edited after the draft read it (its
  `EditSequence` no longer matches).

If QuickBooks does not confirm the change in time, the outcome is logged as `unknown` and the command says so. A
`commit` is never marked as safe to retry: search for the record before trying again, so a change is not saved twice.

### Import and undo

- `qbd import <type> --csv FILE` checks every record like `qbd draft` and returns one token for the batch, up to 200
  records by default and 1000 at most. Each record's outcome goes to the write log. Records saved from the same file
  before are skipped, so the file can be imported again after the rows that failed are fixed.
- `qbd undo <token>` finds a saved change in the write log. An added record is deleted (which needs
  `QBD_ALLOW_DELETE`), and an update puts the old values back. It refuses deletes, voids, changes to lines or to card
  and ID data, and records changed again since; the write log keeps the record as it was, for a person to redo by
  hand.

## Personal data

- In the QuickBooks permission window, leave **Allow this application to access personal data** unticked.
  QuickBooks then withholds Social Security and credit card numbers from this app altogether.
- On top of that, `qbd` masks what it prints (`QBD_MASK_PERSONAL_DATA`, on by default): Social Security, credit
  card, vendor tax ID, bank routing and bank account numbers show only their last four digits, and it leaves out
  payroll details, birth date, gender, ethnicity, disability, military, citizenship and veteran status, card expiry,
  name on card, card security code result and the closing-date password.

## Text in the company file is data

Customer names, memos, item descriptions and notes come from the company file, and anyone who can type into
QuickBooks, or into a form that feeds it, can put text there. `qbd` prints that text as JSON values, and the skills
tell the agent to treat everything in a command's output as data, never as instructions, and to report instructions
it finds there instead of following them.

This lowers the risk; it does not remove it. The approval step for every change is the real protection.

## Keeping QuickBooks responsive

QuickBooks answers one request at a time for everyone using the company file, so a heavy request slows everyone
down. `qbd` keeps requests small:

- commands take turns: one command at a time talks to QuickBooks from this PC, and the next one waits up to 60
  seconds before it gives up with `busy`, which is safe to retry;
- searches return 25 records by default and at most 1000, read through QuickBooks' own iterators, and say
  `truncated` when more match;
- workflow commands read at most 5,000 records per step and say so when they stop early;
- detail reports (general ledger, journal) cover at most one year at a time;
- the Audit Trail report is not run: through the SDK it stopped QuickBooks with an unrecoverable error whenever it had
  rows to send. `qbd changes` lists what was added, changed or deleted instead;
- the Inventory Valuation Summary runs for one item at a time: for every item at once it stopped QuickBooks after a
  long build. The Inventory Asset line of the balance sheet gives the total value of the stock;
- each request times out after `QBD_TIMEOUT_SECONDS` (120). A timeout does not stop a request QuickBooks has
  already started; the command says so and exits without waiting for it;
- each command opens its session and closes it when it ends, so nothing holds the company file open between
  commands.

## Write log

Every committed or attempted change is appended to a JSON-lines file, by default
`%LOCALAPPDATA%\qbd-agent-kit\audit.jsonl`: time, Windows user and PC, company file, token, action, record type,
preview, reason, source, outcome (`done`, `failed`, `unknown`), the IDs QuickBooks returned, the record's values
before an update, delete or void, QuickBooks' error, and the first 4,000 characters of the request with personal
data masked. `qbd log` reads it. The file stays on the PC.

QuickBooks also records these changes in its own **Audit Trail** report, under the QuickBooks user the app worked
as.

## Call log and the activity page

Every command, whether it reads or changes anything, is also appended to the call log, by default
`%LOCALAPPDATA%\qbd-agent-kit\calls.jsonl`: time, Windows user and PC, the command and the arguments it was given,
the outcome and error, how long it took, how many records QuickBooks sent back, and each request sent to QuickBooks
(its name, the elements it used, QuickBooks' status code, the records returned and the time taken). What QuickBooks
sends back is counted, not copied: no names, amounts or other values from its answers go into the call log.
Arguments are kept as given, so a search for a customer keeps that name; text longer than 120 characters is cut,
and a record passed to `draft` keeps only its field names. Each line also names the assistant and its session (as
it named itself, see [setup](setup.md#7-watch-and-approve-in-the-activity-monitor)), the company file, and the
settings the command ran with. Past 5 MB the file moves to `calls.jsonl.<date-time>` and a new one starts; parts
older than 90 days are deleted. `QBD_CALL_LOG=off` turns it off.

`qbd activity --open` makes both logs into a page any web browser opens, for the person who checks the agent's work
without reading logs: the changes saved or tried, with the reason given; what went wrong; and every request the
agent made, day by day. The page is plain HTML with no scripts, kept next to the logs, and like them it never leaves
the PC. The agent's skill tells it to open the page when you ask what it did.

## The Activity Monitor

The [Activity Monitor](setup.md#7-watch-and-approve-in-the-activity-monitor) shows people what the assistants do,
and lets them answer. It is built to be safe to leave running on a shared office PC:

- **It only reads `qbd`'s files.** It never connects to QuickBooks, never sends anything off the PC, and changes
  nothing itself: approving, undoing and stopping run `qbd` (or write the small files `qbd` reads), as the
  signed-in Windows user, so every check of `qbd` applies and the logs say who did it.
- **What is saved is what was shown.** An approval sends the draft's fingerprint as the screen showed it; `qbd`
  refuses the commit if the draft or, for an import, any of its records changed since. Each draft is listed once,
  under the token its file is named for.
- **Settings are still not a lock.** `QBD_REQUIRE_MONITOR_APPROVAL=true` makes `qbd commit` save only changes
  approved in the monitor, so an assistant cannot save one by mistake. An assistant that can run any command can
  still unset it; the write log shows where each change was approved (`approved_in`: `chat` or `monitor`).
- **Text from assistants and QuickBooks stays text.** Titles, names, reasons and errors are shown as plain text,
  never as links or formatting, so a customer name or an assistant's title cannot draw a button or a link.
- **Notifications say who, not what.** "Claude has a change waiting for your approval." or "A QuickBooks window is
  open and waiting for someone.", with no amounts or names, so a locked screen shows nothing from the books.
- **One monitor per Windows user.** A second start only brings the first one's window up, over a local pipe that
  only the same Windows user can open and that takes no other message.
- **It runs `qbd` from the folder the kit is installed in**, so a program named like it in another folder is never
  run in its place.
- **The sample cannot save.** `--sample` shows made-up data, and its Approve buttons stay off.
- **The logs as evidence.** `qbd` only adds to its logs, and moves the call log whole when it rotates. The monitor
  notices a log that got shorter, was replaced, or went away without that move, also while the monitor was closed,
  and shows it in red for a week. This tells you entries may be missing; it does not prove a log was never edited
  (see the gaps below).

## Add-ons

[Add-ons](addons.md), such as the Activity Monitor, are programs a person adds to `qbd` on the PC:

- **Only qbd's catalog installs.** The catalog ships inside `qbd` and names each download by its SHA-256, so a file
  that differs is refused. Any other add-on installs only where an administrator allows its SHA-256, under
  `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\qbd-agent-kit`, and an administrator can turn add-ons off there.
- **Installing runs none of the add-on's code**, and unpacks nothing outside the add-on's own folder.
- **This is the only time `qbd` connects to the internet:** to download an add-on's zip from the kit's GitHub
  releases, when someone installs or updates one. A zip copied to the PC installs with no connection at all.
- **An add-on runs as the person who starts it**, like any program, with the kit's own Python. Each install and
  removal is kept in the call log, with the Windows user.

## SOC 2

SOC 2 is an auditor's report on an organisation's controls, measured against the AICPA's Trust Services Criteria
(2017, points of focus revised in 2022). Software is not SOC 2 compliant by itself: it supplies controls a firm can
rely on, and leaves others to the firm. For a firm whose SOC 2 scope includes an AI assistant's work in QuickBooks
Desktop, this is what `qbd` and the Activity Monitor do in code, and what stays with the firm.

| Criteria | What the kit does | What stays with the firm |
| --- | --- | --- |
| CC2.2, CC2.3 Who is responsible for what | This page and [setup](setup.md) say what is enforced in code and what is not | Telling staff and AI users the rules: never allow `qbd commit` in advance in the AI app |
| CC5.2, CC6.1 Logical access | Read-only until `QBD_ALLOW_WRITES`; changes only for the record types, amounts and actions the settings allow; QuickBooks gives the app only its QuickBooks user's rights; the monitor's pipe opens only for its own Windows user | Windows accounts and QuickBooks users with only the rights each needs; who may sign in to the QuickBooks PC; the app's permission in QuickBooks |
| CC5.1, CC6.1 Segregation of duties | Preparing a change and saving it are separate steps, and the write log keeps where it was approved | **Gap.** An assistant that can run any command as the same Windows user can save its own draft; the settings make that a deliberate act, not a mistake. What holds is the AI app's approval prompt for `qbd commit`, and QuickBooks' user rights |
| CC6.1, CC7.2 Who did it | Every command and change keeps the Windows user and PC (from Windows) and the assistant's name and session (as it gave them) | **Gap.** An assistant's name is what it says; run each automation under its own Windows account so the Windows user tells them apart |
| CC6.7 Data leaving the PC | `qbd` and the monitor send nothing off the PC; `qbd` connects to the internet only to download an add-on someone installs | Sending data to the AI provider is the AI app's doing: check the provider's terms (CC9.2, vendor management) |
| CC6.8 Unauthorised or harmful code | Add-ons install only from qbd's catalog, each download checked against its SHA-256, others only where an administrator's policy allows them; the monitor runs `qbd` from its install folder; text from the company file and from assistants is shown and passed as data, never as links or instructions | Protecting the PC: updates, anti-malware, who can install programs; which add-ons the policy allows, or turning them off |
| CC7.2, CC7.3 Watching for the unusual | The call log keeps every command; the monitor shows what runs now, problems, commands run with other settings than the PC's, assistants with no name, and logs changed outside `qbd` | Someone looks, and acts: incident response (CC7.4, CC7.5) |
| CC7.2 The logs as evidence | `qbd` only adds to its logs; the monitor reports a log made shorter, replaced or removed | **Gap.** The logs are files the Windows user can edit; an edit that keeps a log's length is not noticed. Restrict the state folder with NTFS rights, or forward each line to a store users cannot change |
| CC8.1 Changes to the kit | Versioned releases; tests run every command and the change flow against the sample company; the Windows download and the monitor's add-on keep Python's, pywin32's and wxPython's files as their makers built them | Deciding when to update, and trying an update on the sample company first |
| A1.1 Capacity | Commands take turns, reads are bounded, requests time out, and a command waiting too long gives up with a retryable `busy` | QuickBooks hosting, backups and the PC itself |
| PI1.2 Inputs | Every draft is checked: closing date, names that exist, amount limit, field lengths, duplicates, lines an update would drop | The data the assistant is given, such as the vendor's invoice |
| PI1.3 Processing | The commit sends exactly the previewed request, once, within 15 minutes, to the company file it was drafted on; from the monitor, only if the draft is unchanged since shown; a change QuickBooks did not confirm is marked "Check it", never retried | The person who approves reads the preview |
| PI1.4, PI1.5 Outputs and stored records | The write log keeps each change: outcome, the IDs QuickBooks returned, the values before an update, delete or void; `qbd undo` drafts the reverse | Keeping the write log as long as the firm's retention policy says; backups |
| C1.1 Confidentiality | Personal data masked in output; the call log counts what QuickBooks sends, never copies it; notifications carry no amounts or names | Disk encryption (BitLocker), NTFS rights on the state folder, a locked screen |
| C1.2 Disposal | Drafts expire after 15 minutes; answers are kept 2 days and expired drafts' summaries 7; call log parts are deleted after 90 days | The write log is never deleted by `qbd`: decide how long to keep it |
| CC7.2 Time | Times are written in UTC from the PC's clock | Keeping the PC's clock synchronised |

## QuickBooks' own controls

- **App permission** is given per company file by the Admin, and can be removed at any time in
  **Edit > Preferences > Integrated Applications > Company Preferences**.
- **"Always allow" mode** logs in as a QuickBooks user you choose. Give that user only the rights the agent needs.
- **Closing date and password** protect finished periods; `qbd` also refuses to draft changes in them.
- **QuickBooks recognises the app by its name and by the publisher of the program that connects** (`python.exe`
  from python.org is signed by the Python Software Foundation). Any Python program on this PC that connects as
  *QBD Agent Kit* gets the same access. Protect the PC as you would any computer with access to the books.

## Where data goes

`qbd` runs on your PC and connects to the internet only to download an add-on someone installs. It prints QuickBooks
data for the AI agent that ran it, and the agent's app sends that to its AI model. Check how your AI provider stores
and uses what you send before connecting real books.

## Reporting a security problem

Please report security problems privately through
[GitHub's private vulnerability reporting](https://github.com/ph4mHoa/qbd-agent-kit/security/advisories/new), not
in a public issue.
