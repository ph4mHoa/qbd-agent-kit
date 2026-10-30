---
name: enter-bill-from-document
description: Turn a vendor invoice (PDF, photo, email text) into a QuickBooks Desktop bill, or a customer order into an invoice or sales order - read the document, match the vendor, items and accounts (qbd coding shows how the vendor is usually coded), check for duplicates, and draft it with qbd draft for the person's approval. Use for "enter this bill", "add this invoice to QuickBooks", "record this receipt".
---

# Enter a bill from a document

Needs `qbd status` to show `writes.enabled`, with `bill` (or `invoice` / `sales_order`) among the record types.

## Steps

1. **Read the document.** Write down: vendor name, bill number, bill date, due date or terms, each line
   (description, quantity, unit price, amount), tax, total. If any of these is unreadable, ask; never guess an
   amount.
2. **Match the vendor.** `qbd search vendor --name "<a distinctive word>"`. If there is no match, ask whether to
   add the vendor (a separate `qbd draft add vendor`) or which existing one it is.
3. **Check it is not already entered.** `qbd search bill --party "<vendor>" --ref-number "<bill number>"`. If
   found, stop and show it.
4. **Choose lines.** `qbd coding "<vendor>"` shows the accounts and items this vendor's bills usually go to, and
   the accounts QuickBooks pre-fills for it. Use those unless the person says otherwise.
   - Stock the business resells: `ItemLineAdd` with `ItemRef`, `Quantity`, `Cost`. Find items with
     `qbd search item --name "<word>"`.
   - Services and expenses: `ExpenseLineAdd` with `AccountRef` (an expense account; `qbd search account
     --account-type Expense`) and `Amount`.
5. **Draft.** Write the fields to a file and run
   `qbd draft add bill --fields @bill.json --reason "Vendor invoice" --source "<the document's file name>"`:
   ```json
   {"VendorRef": "Coastal Freight Lines", "RefNumber": "CF-1190", "TxnDate": "2026-10-18", "DueDate": "2026-11-02",
    "ExpenseLineAdd": [{"AccountRef": "Freight and Delivery", "Amount": 486.20, "Memo": "Oct deliveries"}]}
   ```
   `--source` makes `qbd` refuse the same document if it was already saved.
6. **Compare and approve.** Show the preview next to the document's total. If they differ (tax, rounding,
   shipping), say by how much and fix the draft before asking for approval. Run `qbd commit <token>` only after a
   clear yes.

## Customer orders

The same steps work for a customer purchase order: match the customer, use `InvoiceLineAdd` or
`SalesOrderLineAdd` with `ItemRef`, `Quantity` and `Rate`, put the customer's PO number in `PONumber`, and draft
type `invoice` or `sales_order`. Without a `Rate`, the preview shows the item's price that QuickBooks will fill in.

## Many documents

For a spreadsheet of bills or invoices, `qbd import bill --csv <file>` checks every row like a draft and returns
one token for the batch; `qbd help import` lists the columns.
