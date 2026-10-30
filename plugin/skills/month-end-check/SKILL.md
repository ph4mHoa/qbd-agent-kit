---
name: month-end-check
description: Check a QuickBooks Desktop company file before closing a month with qbd health-check, find-duplicates, uncleared and compare-periods - uncategorized amounts, undeposited payments, unapplied payments, duplicates, uncleared bank items, old receivables and payables, negative stock, stale orders, future dates - and explain each fix. Use for "month end", "close the books", "clean up", "is everything right", "before the accountant".
---

# Month-end check

## Steps

1. Ask which month, or use last month. Run `qbd health-check --period-end <the month's last day>`.
2. Run `qbd find-duplicates --from <first day> --to <last day>`.
3. Run `qbd uncleared --older-than-days 30` for checks never cashed and deposits the bank never saw.
4. Run `qbd compare-periods --current-from <first day> --current-to <last day>` (it compares with the month
   before), and look only at `biggest_changes`.
5. Report:
   - **Fix before closing** (warnings), most money first. For each: what it is, the amount, and where to fix it
     in QuickBooks (the `fix` line), with up to five example records.
   - **Worth a look** (info items, medium-confidence duplicates, old uncleared items, unusual swings in the
     comparison).
   - **Looks fine** in one line listing the checks that passed.
6. When everything is fixed, remind the person to move the closing date forward
   (Edit > Preferences > Accounting > Company Preferences > Set Date/Password). `qbd` cannot set it.

## Notes for explaining

- *Undeposited Funds:* payments received but not yet put on a bank deposit. Fix: Banking > Make Deposits,
  matching the bank statement; `qbd deposits --amount <the bank's figure>` finds which payments make up a deposit.
- *Unapplied payments:* money received but not matched to an invoice. Fix: Customers > Receive Payments.
- *Unapplied vendor credits:* credits or payments to a vendor not matched to a bill. Fix: apply them in
  Vendors > Pay Bills when paying that vendor's next bills.
- *Opening Balance Equity / uncategorized:* amounts parked during setup or imports. Reclassify with the
  accountant.
- *Negative stock:* sold before the purchase was entered. `qbd stock-history "<item>"` lists each time the item
  went below zero and what brought it back. Fix each time, not only today's quantity: date the purchase on or
  before the sale if the goods were there, enter a missing receipt, or count and adjust; agree date changes with
  the accountant.
- *Uncleared items:* an old uncleared check may need voiding and reissuing; an uncleared deposit with a twin may be
  entered twice. Decide with the bank statement in hand.
- *A beginning balance that no longer matches the bank:* a reconciled transaction was changed, voided or deleted
  after its reconciliation. `qbd reconcile-check <account> --statement-date <last statement's end>
  --bank-balance <the bank's balance then>` lists the likely ones.
- *Sales tax that does not tie out:* what Pay Sales Tax shows as owed differs from Sales Tax Payable on the balance
  sheet. `qbd sales-tax-check --as-of <the month's last day>` compares them and lists sales tax paid by check, bill
  or card instead of Pay Sales Tax, and journal entries to the account without a vendor.
- *Foreign-currency balances (multicurrency files):* the books carry them at their transactions' rates.
  `qbd currency --rates <CODE>=<the month-end rate>` shows each foreign account's gain or loss at that rate, the
  customers and vendors paid in full that still carry a home-currency balance, and transactions entered at a rate
  of 1 or upside down. The home currency adjustment itself is the accountant's.
- A matching amount is not proof of a duplicate. Ask the person to open both records before deleting anything.

## Do not

Do not delete, void or change anything during the check. If the person wants a fix made, handle it as a
separate change with `qbd draft` and their approval.
