---
name: ar-collections
description: Plan collections for overdue customer invoices in QuickBooks Desktop with qbd receivables - who to chase first, what to say, and reminder emails that fit how late each customer is - and match incoming payments to invoices with qbd match-payment. Use for "who owes us", "chase invoices", "collections", "overdue customers", "payment reminders", "which invoices did this payment pay".
---

# Collections

## Steps

1. Run `qbd receivables` (add `--customer "<full name>"` for one customer). When the person is at this PC, add
   `--open`: their web browser shows who owes and how late, oldest money first, with each customer's invoices.
2. Before chasing anyone with `unapplied_payments_or_credits`, say so: that money may already cover an invoice.
   Suggest applying it in QuickBooks (Customers > Receive Payments) first. Customers under
   `customers_in_credit` have payments or credits that cover all their invoices: nothing to chase.
3. List customers in the order returned (most overdue money first): customer, overdue amount, oldest days late,
   next step.
4. Draft one email per customer the person wants to chase. Use the invoice list (number, date, due date,
   balance) and the contact name. When an invoice has `balance_in_currency`, quote that amount and `currency`:
   `balance` is in the company's home currency. Match the tone to `next_step`:

| next_step starts with | Tone |
| --- | --- |
| friendly reminder (up to 15 days late) | Light. "Just a reminder that invoice 1094 for 618.50 was due on June 26." |
| second reminder (16-45 days) | Clear. Restate the amounts, ask for a payment date. |
| phone call (46-90 days) | Suggest calling; give the person three talking points and a payment date to agree. |
| final notice (over 90 days) | Firm and polite. State the total, the days late and what happens next (as the person decides). |

5. Do not send anything. Give the drafts to the person to send from their own email.

## When a payment comes in

`qbd match-payment "<customer>" <amount>` (add `--memo "<remittance text>"` when there is one) gives the open
invoices it most likely pays and how sure that is. Show the match; when changes are on and the person agrees, draft
the payment from its `draft_fields` with `qbd draft add receive_payment` and commit only after their yes.

## Rules

- Never threaten legal action, fees or credit reporting unless the person tells you to and words it.
- Never invent invoice numbers, amounts or dates; use only what the command returned.
- If a customer has many invoices, list the oldest five and the total.
- Amounts under 1.00 overdue are usually rounding; mention them only at the end.
