# Privacy

The Agent Kit for QuickBooks Desktop (the `qbd` command line, its agent skills and its add-ons) runs on your PC. It has
no account, no server and no telemetry, and it sends nothing to its authors.

## What it reads

`qbd` reads what each command needs from the QuickBooks company file open on the same PC, through the SDK that
QuickBooks installs: customers, vendors, items, transactions and reports. It prints the answer as one line of JSON for
the agent that ran it.

## Where that data goes

The agent's app sends that JSON to its AI model to answer you, as it does with anything else in the conversation.
Check how your AI provider stores and uses what you send before connecting real books.

`qbd` connects to the internet only to download an add-on someone installs from its catalog, from the kit's GitHub
releases.

## What it keeps, and for how long

Everything stays on the PC, in `%LOCALAPPDATA%\qbd-agent-kit` (on other systems, `$XDG_STATE_HOME/qbd-agent-kit` or
`~/.local/state/qbd-agent-kit`):

| File | What | Kept |
| --- | --- | --- |
| `audit.jsonl` | The write log: every change saved or tried, with the values a record had before an update, delete or void, and the start of the request with personal data masked | Until you delete it: `qbd` never does, so decide how long your records need it |
| `calls.jsonl` | The call log: every command with its arguments as given (a customer name you searched for stays), how long it took and how many records came back, never the values QuickBooks sent | Past 5 MB it is moved aside; parts older than 90 days are deleted. `QBD_CALL_LOG=off` turns it off |
| `drafts\` | Changes waiting for approval | 15 minutes; a person's answer in the Activity Monitor 2 days; the summary of a draft nobody answered 7 days |
| `activity.html` | The page `qbd activity` makes from the two logs | Rewritten each time `qbd activity` runs |
| `cash-position.html`, `receivables.html` | The pages `qbd cash-position --open` and `qbd receivables --open` draw: cash, invoices and bills, with customer and vendor names | Rewritten each time the command runs with `--open` |
| `quickbooks.json` | The last facts read about each company file: product, version, country, closing date | Updated each time they are read |

The other files there (`live\`, `paused\`, `monitor.json`, `quickbooks.lock`) let commands and the Activity Monitor
work together and hold no values from the company file. The sample company (`--demo`) keeps its own logs,
`audit-demo.jsonl` and `calls-demo.jsonl`.

To remove everything, delete that folder.

## Contact

Questions: [GitHub issues](https://github.com/ph4mHoa/qbd-agent-kit/issues). Security problems:
[SECURITY.md](SECURITY.md).
