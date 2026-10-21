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
2. **Approve.** The agent shows you the preview and the warnings. You say yes or no.
3. **Commit.** `qbd commit <token>` sends exactly the request that was previewed. `qbd` keeps the draft in its own
   folder, so the agent cannot change it between preview and commit.
4. **Log.** The outcome goes to the write log, which `qbd log` reads.
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
- the Audit Trail report is not run: through the SDK it stopped QuickBooks with an unrecoverable error (with a
  modified-date filter, and on a day with transactions added and deleted). `qbd changes` lists what was added,
  changed or deleted instead;
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

## QuickBooks' own controls

- **App permission** is given per company file by the Admin, and can be removed at any time in
  **Edit > Preferences > Integrated Applications > Company Preferences**.
- **"Always allow" mode** logs in as a QuickBooks user you choose. Give that user only the rights the agent needs.
- **Closing date and password** protect finished periods; `qbd` also refuses to draft changes in them.
- **QuickBooks recognises the app by its name and by the publisher of the program that connects** (`python.exe`
  from python.org is signed by the Python Software Foundation). Any Python program on this PC that connects as
  *QBD Agent Kit* gets the same access. Protect the PC as you would any computer with access to the books.

## Where data goes

`qbd` runs on your PC and never connects to the internet. It prints QuickBooks data for the AI agent that ran it,
and the agent's app sends that to its AI model. Check how your AI provider stores and uses what you send before
connecting real books.

## Reporting a security problem

Please report security problems privately through
[GitHub's private vulnerability reporting](https://github.com/ph4mHoa/qbd-agent-kit/security/advisories/new), not
in a public issue.
