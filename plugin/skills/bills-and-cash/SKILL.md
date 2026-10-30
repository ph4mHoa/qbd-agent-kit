---
name: bills-and-cash
description: Plan which vendor bills to pay and check that cash covers them, from QuickBooks Desktop open bills (qbd payables) and a short cash forecast (qbd cash-position). Use for "what bills are due", "payment run", "can we afford", "cash flow", "cash forecast", "will we run short".
---

# Bills and cash

## Steps

1. Run `qbd payables` (bills due in the next 14 days; `--days-ahead N` to change it) and `qbd cash-position`
   (30 days ahead). When the person is at this PC, add `--open` to `qbd cash-position`: their web browser shows the
   forecast as a figure (cash at each week's end, money in and out, the lowest point, the bills behind each week).
   Then keep your reply to what the figure does not say: the payment run and what to decide.
2. Ask, if you do not know it, the lowest bank balance the person wants to keep.
3. Propose a payment run:
   - **Pay now:** overdue bills, oldest first, and bills due within the week.
   - **Can wait:** bills due later in the window.
   - **Check first:** vendors with `unapplied_credits` (apply them in Pay Bills when paying), bills that
     `qbd find-duplicates --include bills` flags (do not pay a possible duplicate before checking), and bills whose
     cost or quantity differs from their purchase order (`three_way_match` in `qbd purchasing`).

   A bill's `amount` is what is left once the vendor's unapplied credits are used on its oldest bills;
   `open_on_bill` is what QuickBooks shows open on the bill itself. Lines with a `type` (Item Receipt, General
   Journal...) are not bills but still count in what the vendor is owed.
4. Show the cash effect: starting cash, total of the proposed run, and the forecast's `lowest_point`. If the
   lowest point falls under the person's minimum, say which bills to move and by how much.

## Saying what the forecast is

The forecast is built from due dates only. Always add one line: it does not include payroll, card spending,
loan payments or costs not entered as bills, and assumes customers pay on time (overdue invoices are left out;
`qbd cash-position --count-overdue-receivables` counts them in the first week if the person asks).

## Recording payments

Paying bills is done in QuickBooks (Vendors > Pay Bills) so checks print and bank accounts post correctly.
Do not draft bill payments unless the owner allowed `bill_payment_check` in `QBD_WRITE_ENTITIES` and asks for it.
