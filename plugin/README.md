# Agent Kit for QuickBooks Desktop: agent skills

Nine agent skills that teach Claude to work with QuickBooks Desktop through `qbd`, a command line that runs on the
Windows PC that hosts QuickBooks. Claude reads the books, answers with QuickBooks' own figures, and drafts changes that
are saved only after a person approves them.

## What you need

- QuickBooks Desktop on a Windows PC, with `qbd` installed on that PC: see the
  [setup guide](https://github.com/ph4mHoa/qbd-agent-kit/blob/main/docs/setup.md).
- Claude Code, or another agent that can run commands, on that same PC, so it can run `qbd`.

No QuickBooks at hand? `--demo` answers from a built-in sample company on any computer with Python, for example
`qbd daily-brief --demo --pretty`.

## Install

In Claude Code 2.1.275 or later:

```
/plugin install qbd-agent-kit --marketplace ph4mHoa/qbd-agent-kit
```

On older versions: `claude plugin marketplace add ph4mHoa/qbd-agent-kit`, then
`claude plugin install qbd-agent-kit@qbd-agent-kit`.

## What you can ask

- *"Give me today's finance brief."*
- *"Who should I chase for payment?"*
- *"What bills are due, and can we afford them?"*
- *"What should I reorder from Metro Electric?"*
- *"Check the books before I close September."*
- *"Enter this bill."* with a PDF: a checked draft and a preview, saved only after you say yes.

## The skills

| Skill | What it does |
| --- | --- |
| quickbooks-desktop | The basics: which command to use, bounded requests, the draft and approve rules |
| daily-finance-brief | A short morning summary with today's actions |
| ar-collections | Who to chase, and reminder emails by stage |
| bills-and-cash | A payment run checked against cash |
| month-end-check | What to fix before closing a month |
| inventory-reorder | Reorder quantities and purchase orders per vendor |
| ask-the-books | Picking the right report for any question |
| enter-bill-from-document | A vendor invoice into a bill, checked and approved |
| support-case | What is wrong with QuickBooks, the file or a sync app, who fixes it, and the note to send them |

## Safety

`qbd` is read-only until the owner turns changes on. A change is checked, previewed with what QuickBooks will fill in,
saved with a one-time token only after a person approves it, logged, and can be undone. The kit cannot send or move
money: it reads QuickBooks and records changes in QuickBooks, each one only after a person approves it.
[How it is kept safe](https://github.com/ph4mHoa/qbd-agent-kit/blob/main/docs/safety.md).

## What it runs, sends and fetches

- The plugin is text: nine `SKILL.md` files. It has no hooks, no MCP server and no code of its own.
- The skills tell Claude to run `qbd` commands on the PC. `qbd` talks to QuickBooks through the SDK that QuickBooks
  installs on that PC and prints one line of JSON, which Claude reads; like anything else in the conversation, it goes
  to the AI model.
- `qbd` keeps its logs and drafts on the PC and sends nothing to the kit's authors. It connects to the internet only to
  download an add-on someone installs from its catalog, from the kit's GitHub releases.

What it keeps, and for how long: [privacy](https://github.com/ph4mHoa/qbd-agent-kit/blob/main/PRIVACY.md).

## Help

Questions and problems: [GitHub issues](https://github.com/ph4mHoa/qbd-agent-kit/issues). Security problems:
[report them privately](https://github.com/ph4mHoa/qbd-agent-kit/security/advisories/new).

[MIT](https://github.com/ph4mHoa/qbd-agent-kit/blob/main/LICENSE). QuickBooks is a trademark of Intuit Inc. This
project is not affiliated with or endorsed by Intuit.
