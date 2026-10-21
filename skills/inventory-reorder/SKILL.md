---
name: inventory-reorder
description: Decide what stock to reorder from QuickBooks Desktop inventory (reorder points, stock on order, open sales orders, recent sales speed) with qbd reorder, stock, backorders and stock-history, and, when changes are allowed, draft purchase orders per vendor for approval. Use for "what should I reorder", "low stock", "purchase orders", "running out", "what can ship".
---

# Inventory reorder

## Steps

1. Run `qbd reorder`. Use `--vendor "<name>"` when the person orders from one supplier, `--cover-days N` for how
   long the order should last (default 30), and a longer `--lookback-days N` for slow movers.
2. Present one block per vendor: item, available (on hand + on order - on sales orders), days of cover,
   suggested quantity, estimated cost; vendor total at the bottom. Most urgent first.
3. Call out separately:
   - items with negative stock (fix the count before trusting the suggestion; `qbd stock-history "<item>"` shows
     what took it there);
   - items with sales but no reorder point (suggest setting one in QuickBooks);
   - items with `(no preferred vendor)`.
4. Ask the person to adjust quantities: pack sizes, minimum orders, lead times, seasonal peaks the history does
   not show.

For stock per site, or only the items that are short, use `qbd stock --short`. For which open sales orders can ship
now and which wait on a purchase order, use `qbd backorders`. Purchase orders that are late or partly received are in
`qbd purchasing`; mention them before ordering more of the same item.

## Drafting purchase orders

Only if `qbd status` shows `writes.enabled` with `purchase_order` among the record types.

1. One draft per vendor: `qbd draft add purchase_order --fields @po.json --reason "Reorder <date>"` with
   `{"VendorRef": "<vendor>", "PurchaseOrderLineAdd": [{"ItemRef": "<item>", "Quantity": 40, "Rate": 19.80}]}`,
   using the agreed quantities and the `unit_cost` from `qbd reorder`.
2. Show each preview; commit only the ones the person approves.
3. List the saved PO numbers at the end. Sending the PO to the vendor is done from QuickBooks.
