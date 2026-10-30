# Compared with other options

Checked against each product's own website, documentation or repository: the agent and API options on 20 October
2026 (Conductor and CData again on 21 October), the single-job tools on 21 October 2026. Prices and features change,
so follow the links before deciding. Corrections are welcome as issues or pull requests.

## Ways to connect an AI agent to QuickBooks Desktop

| Option | How it reaches QuickBooks Desktop | Data passes through | Price, as listed | Changes to the books |
| --- | --- | --- | --- | --- |
| **qbd-agent-kit** (this) | QuickBooks SDK on the same PC, through Python | Your PC, then your AI app | Free, MIT | Off by default; checked draft, your approval, then commit; undo |
| [Conductor](https://docs.conductor.is/usage/mcp) | QuickBooks Web Connector to Conductor's API; hosted MCP server | Conductor's cloud | [$49 a month per company file connection in use; sample company files and trials free](https://docs.conductor.is/faq) | Read and write; read-only mode set per connection |
| [Desktop Accounting API](https://github.com/DesktopAccountingAPI/quickbooks-desktop-mcp) | QuickBooks Web Connector to a hosted API; hosted or local MCP server | Their cloud | [$49 per active company file a month](https://www.desktopaccountingapi.com/pricing/), 30-day trial | Read-only keys; confirmation and idempotency keys for writes |
| [Kipper](https://kipper.com/quickbooks-mcp-connector/) | Desktop connector to a hosted service | Their cloud | [From $200 a month for two companies, plus $50 a month per Desktop company file](https://kipper.com/pricing/) | Read-only |
| [Apideck](https://www.apideck.com/integrations/quickbooks-desktop) | Hosted unified Accounting API with a hosted MCP server | Their cloud | [From $599 a month for 25 active consumers](https://www.apideck.com/pricing), 30-day trial | Read and write through unified resources |
| [CData](https://www.cdata.com/drivers/quickbooks/mcp/) | QuickBooks Gateway on the QuickBooks PC, used by CData's drivers | Your PC, or CData Connect AI | [ODBC driver $499 a year per desktop](https://www.cdata.com/drivers/quickbooks/order/odbc/); [Connect AI from $79 a month billed yearly](https://www.cdata.com/ai/pricing/); the [open-source server](https://github.com/CDataSoftware/quickbooks-mcp-server-by-cdata) is read-only and needs a driver | SQL; read-only in the open-source server |
| [Activepieces](https://www.activepieces.com/mcp/quickbooks-desktop-conductor) | Through Conductor | Activepieces and Conductor | Activepieces has a free plan; Conductor is charged separately | 8 tools and 2 triggers |
| [devwithmj/quickbooks-mcp](https://github.com/devwithmj/quickbooks-mcp) | QuickBooks SDK on the same PC (C#) | Your PC | Free, MIT, pre-1.0 | Read-only default; previews, confirmation tokens, idempotency keys, audit log |
| [RealDealCPA-VR/Quickbooks-MCP-Desktop](https://github.com/RealDealCPA-VR/Quickbooks-MCP-Desktop) | QuickBooks SDK on the same PC (Node.js) | Your PC | No licence file | 154 tools with confirmation and an audit log |
| [lilfroog/qbbridge](https://github.com/lilfroog/qbbridge) | QuickBooks SDK on the same PC (Python) | Your PC | Free, MIT | 13 tools with an undo log |

QuickBooks Online is a different product with a cloud API. Intuit publishes an official
[QuickBooks Online MCP server](https://github.com/intuit/quickbooks-online-mcp-server), and Zapier, Composio,
Pipedream and n8n connect to QuickBooks Online only.

## Paid tools that do one job

Many QuickBooks Desktop users pay for a tool per job. Prices as listed on 21 October 2026:

| Tool | Job | Price, as listed |
| --- | --- | --- |
| [Transaction Pro](https://www.rightworks.com/plans/rightworks-transaction-pro-pricing/) | Import, export, delete | $199 (importer) or $299 (importer, exporter and deleter), one-time per user; $99 a year for support after the first year |
| [SaasAnt Transactions](https://www.saasant.com/pricing/saasant-transactions-desktop/) | Import, export, delete | $125 a year (Starter, one computer) to $299 a year (Migration); server editions $299-$599 a year |
| [Zed Axis](https://www.zed-systems.com/axis-buy.html) | Import, export | $155 a year (import), $255 (import and export), $350 (with automation) |
| [QQube](https://clearify.com/qqube/qqube-for-quickbooks-pricing/) | Data warehouse for Excel and BI | $550 the first year and $195 a year after (one workstation); more for shared servers |
| [QODBC](https://qodbc.com/buy-qodbc-desktop-subscription/) | SQL access through ODBC | $149 a year read-only, $199 read-write; a read-only QODBC comes with Enterprise |
| [Webgility](https://www.webgility.com/pricing) | E-commerce sync | From $249 a month, or $199 a month billed yearly |

`qbd` covers part of the first two jobs, locally and free: a checked CSV import saved after one approval
(`qbd import`), and a CSV export of any list or transaction type (`qbd export`). It has no data warehouse, no SQL
access, no bulk delete and no e-commerce sync.

## What this kit rebuilt from the paid products

| In paid products | Where | In this kit |
| --- | --- | --- |
| Ask about overdue invoices, payment history, sales | Conductor, Kipper | `qbd receivables`, `qbd search`, `qbd report` and the `ask-the-books` skill |
| Search the API docs or schema (`search_docs`, `get_api_endpoint_schema`) | Conductor, Desktop Accounting API | `qbd schema`, generated from the SDK's qbXML schema and available offline |
| Run any request (`execute`, `invoke_api_endpoint`, Custom API Call) | Conductor, Desktop Accounting API, Activepieces | `qbd raw` for queries; `qbd draft` for changes |
| Upsert Customer, Upsert Vendor | Activepieces | `qbd draft upsert customer` |
| Create Invoice, Create Bill, Record Payment | Activepieces | `qbd draft add` with type `invoice`, `bill`, `receive_payment` |
| Query Transactions, List Items | Activepieces | `qbd search` |
| Reports (aging, profit and loss, custom) | Conductor | `qbd report`: 35 named reports and any other report type |
| Read-only mode | Conductor, Desktop Accounting API, Kipper | The default; changes only with `QBD_ALLOW_WRITES=true` |
| Confirmation before writes, no double writes | Desktop Accounting API | Draft, approval, single-use token, and a source key that refuses a document entered before; an unknown outcome makes the agent search before retrying |
| Cash flow forecasting, AR aging, management reporting | Kipper | `qbd cash-position`, `qbd receivables`, `qbd daily-brief`, `qbd compare-periods` |
| Request or audit log | Desktop Accounting API, Kipper | A local call log of every command and write log of every change, shown as a page by `qbd activity`; `qbd log` and `qbd undo` |
| Import from a spreadsheet | Transaction Pro, SaasAnt, Zed Axis | `qbd import`: every row checked like a draft, one approval, safe to run again |
| Export to Excel or BI | Transaction Pro, SaasAnt, Zed Axis, QQube, QODBC | `qbd export` to CSV |

On top of that, the kit has workflows built for owners, bookkeepers and buyers: a month-end health check,
duplicate detection (bills entered twice, bills also paid by check, double billing, duplicate names), following a
document through its chain, matching a customer payment to invoices and a bank deposit to payments, a buyer's
worklist with a three-way match, stock and backorders, what took an item below zero, a 1099 worklist, uncleared bank items, why a
reconciliation's beginning balance moved, sales priced below the price list, sales tax owed against the books, foreign-currency balances at today's rate, how a vendor is coded,
reorder suggestions per vendor, a daily brief, a check of the apps that sync through the Web Connector and
what their errors mean, and agent skills that turn each into a repeatable routine.

## Not covered

- **AI apps on another computer or in the browser.** `qbd` runs on the QuickBooks PC, for an agent that can run
  commands on that PC. claude.ai or ChatGPT in a browser, or an agent on a server, need a hosted option such as
  Conductor or Desktop Accounting API.
- **Many company files at once.** `qbd` works with the one file open in QuickBooks, or the one in
  `QBD_COMPANY_FILE`.
- **Event triggers** such as "new invoice" or "new payment". `qbd changes <since>` lists what was added, changed or
  deleted since a time, for an agent or script that checks on a schedule.
- **Running payroll, downloading bank feeds and finishing a reconciliation.** The QuickBooks SDK cannot do these;
  `qbd uncleared` helps before you reconcile, and `qbd reconcile-check` finds why a beginning balance moved.
- **SQL access, a data warehouse, bulk delete and e-commerce sync.** See the single-job tools above.

## Which to choose

- **One business, QuickBooks on one PC, an agent that can run commands on that PC:** this kit, or one of the other
  local open-source servers.
- **AI app in the browser or on a server, or many company files on many PCs:** a hosted product such as Conductor
  or Desktop Accounting API.
- **Building a product for many QuickBooks Desktop customers:** Conductor, Desktop Accounting API or Apideck,
  which handle connection setup for your customers.
- **Questions only, no changes, across many companies and systems:** Kipper.
