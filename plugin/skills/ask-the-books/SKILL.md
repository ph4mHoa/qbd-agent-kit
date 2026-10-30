---
name: ask-the-books
description: Answer free-form questions about a business from its QuickBooks Desktop data - sales, profit, expenses, balances, customers, vendors, items, tax - by choosing the right qbd report or record search and showing the figures. Use for any "how much", "who", "which", "why", "top", "compare" question about the books.
---

# Ask the books

## Pick the source

| Question | Run |
| --- | --- |
| Sales for a period, top customers | `qbd report sales_by_customer` with `--period` or `--from`/`--to` |
| Best-selling items, quantities | `qbd report sales_by_item` |
| Profit, margin | `qbd report profit_and_loss`; by month: `--columns Month` |
| Why profit or a cost changed | `qbd compare-periods` |
| Spending by vendor | `qbd report expenses_by_vendor` |
| What we own and owe on a date | `qbd report balance_sheet --to <date>` |
| Who owes us / what we owe | `qbd receivables` / `qbd payables`, or the `ar_aging` / `ap_aging` reports |
| Stock value | The Inventory Asset line of `qbd report balance_sheet --to <date>`; for one item, `qbd report inventory_valuation --item "<item>"` |
| Why an item's stock or cost is off | `qbd stock-history "<item>"` |
| Sales tax due | `qbd report sales_tax_liability` |
| One customer's history | `qbd search <type> --party "<customer>"` with dates, then `qbd get` for detail |
| One document ("invoice 1042") | `qbd search invoice --ref-number 1042`, then `qbd get invoice <TxnID>`; `qbd trace 1042` for what it is linked to |
| How many of something | `qbd count <type>` with the same filters as a search |

## Answer

1. One or two sentences answering the question, with the number.
2. The figures behind it (a short table is fine here), the period, and the basis (accrual or cash).
3. If the question was ambiguous (which year? fiscal or calendar? with or without tax?), state the choice you
   made and offer the other.

## Rules

- Never add up search results when a report gives the total.
- Reports: the last column is usually the total. Rows are indented by level; "Total ..." rows are subtotals.
- For periods use QuickBooks names (`--period ThisMonthToDate`, `LastMonth`, `ThisFiscalYearToDate`,
  `LastCalendarYear`) or explicit dates. "This year" usually means the fiscal year; check
  `fiscal_year_starts_month` in `qbd status`.
- Detail reports (general ledger, journal) can be huge. Ask for a narrow date range first.
- For who changed what, use `qbd changes <since>`: the Audit Trail report is refused, because through the SDK it
  stopped QuickBooks.
- If a report comes back with no rows, its `note` says why (nothing in the period, or no budget for that year);
  pass that on instead of guessing.
- If the data cannot answer the question (budgets not set up, no classes), say so plainly.
