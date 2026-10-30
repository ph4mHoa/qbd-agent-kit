---
name: support-case
description: When QuickBooks Desktop, the company file or an app that syncs with it misbehaves, collect the facts with qbd doctor, status, file-health and web-connector, explain them in plain words, say who can fix it (the person, Intuit, the app's vendor, or whoever runs the PC and network), and write the note to send them. Use for "QuickBooks is slow", "an error", "it crashed", "the sync stopped", "Web Connector", "before a Rebuild or an upgrade", "who do I call", "write to support".
---

# Support case

## Steps

1. Run `qbd doctor`. When it cannot connect, its hint comes first: QuickBooks closed, a window open in QuickBooks
   waiting for an answer, or permission not given yet.
2. Run `qbd status` and note the QuickBooks version and `intuit_support`. A version Intuit no longer supports gets
   no security updates or live support, and its bank feeds, payroll and payments stop.
3. Run `qbd file-health`: file and transaction log size, a file opened over the network, backups, QuickBooks' own
   errors, its services and free disk.
4. When an app syncs through the Web Connector (a store, a CRM, a payroll or time service), run `qbd web-connector`.
   Each app's `last_result`, `last_error` and `what_it_means` say what failed.
5. Report in plain words what is wrong, what it means, and who fixes it:

| Finding | Who fixes it |
| --- | --- |
| A Web Connector error that `what_it_means` puts on the app's server (QBWC1012, QBWC1036, QBWC1041 to QBWC1045, a web page instead of the service) | The app's vendor: send them the note |
| A Web Connector app with a wrong or missing password | The person, with the password from the app's vendor |
| A Web Connector app tied to a company file that moved or was renamed | The person: put the file back, or add the app again for the file in use |
| QuickBooks errors in its log, a damaged file, Verify or Rebuild | The person or their accountant, backup first; Intuit support for errors that stay |
| The file on a network drive, slow multi-user work, QuickBooks services stopped, a full disk | Whoever runs the PC and the network |
| QuickBooks will not open: "Could not initialize license properties" (error 3371) | The person: Intuit's fix is the QuickBooks Tool Hub (Installation Issues > 3371 Error Fix), then reinstalling QuickBooks; whoever runs the PC if it stays |
| A version Intuit no longer supports, or soon will not | The owner: plan the upgrade |

6. Write the note to send, ready to paste: what happened and since when, the QuickBooks version, each error code
   with its text and time, and what was tried. Leave out customer and vendor names, amounts and passwords.

## Around a Rebuild, an upgrade or a condense

- Before: the person makes a backup in QuickBooks (File > Back Up Company). Then run
  `qbd snapshot --out before.json` and `qbd file-health`.
- After: run `qbd snapshot --compare before.json`. Balances that moved mean something changed: keep the backup and
  show the comparison to the accountant before working on in the file.

## Do not

- Do not change, delete or rebuild anything. Verify, Rebuild, upgrades and Web Connector settings are done by a
  person, in QuickBooks and the Web Connector.
- Do not contact a vendor or Intuit yourself: give the person the note.
