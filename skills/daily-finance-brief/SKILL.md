---
name: daily-finance-brief
description: Give a business owner a short morning summary of their QuickBooks Desktop books (cash, overdue customers, bills due, sales against last month, low stock) with the few actions that matter today, using qbd daily-brief. Use for "daily brief", "how are we doing", "morning summary", "what needs my attention".
---

# Daily finance brief

## Steps

1. Run `qbd daily-brief` (bills due in the next 7 days; add `--upcoming-days 14` on Mondays or after a holiday if
   the person asks).
2. Write the brief in this order, in plain words, at most about 200 words:
   - **Cash:** bank total; mention Undeposited Funds if it is not zero ("make a deposit"; `qbd deposits` lists
     what is waiting).
   - **Owed to us:** total and overdue; name the two or three biggest overdue customers with amounts.
   - **We owe:** overdue bills and what falls due this week.
   - **Sales:** month to date against the same days last month, as a percentage. Do not explain a change
     unless the person asks; offer `qbd compare-periods` instead.
   - **Stock:** how many items are at or below reorder point; any negative stock.
3. End with **Today:** the three most useful actions, taken from `attention`, most money first.
4. If the result has `errors`, say which part could not be checked and why, in one line.

## Style

- Lead with numbers, not adjectives. "14,824.75 overdue from 7 customers", not "receivables look concerning".
- No tables unless the person asks; this is read on a phone.
- If nothing needs attention, say so in one line.

## Example

> **Cash** 168,250 in the bank; 13,123 waiting to be deposited.
> **Owed to us** 21,173, of which 14,825 is overdue (Foxhall Landscaping 4,067, Granite Peak 2,975).
> **We owe** 3,322 overdue across 6 bills; 2,771 due this week.
> **Sales** 9,233 this month so far, 38% below the same days last month.
> **Stock** 5 items at reorder point; LED Flood Light 50W shows -3 on hand.
>
> **Today:** deposit the 13,123; call Foxhall and Granite Peak; run the reorder list for Metro Electric.
