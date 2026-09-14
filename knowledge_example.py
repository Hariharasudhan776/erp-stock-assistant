"""TEMPLATE for the assistant's schema knowledge.

Copy this file to `knowledge.py` and replace the example tables with your own. The text
becomes the (cached) system prompt, so:
  - describe tables, join keys and business rules in plain words the model can act on;
  - name the traps (misleading column names, flags that must be filtered, bad data);
  - give two or three ready-made query shapes for the most common questions;
  - keep it free of anything that changes per request (dates, ids), or the cache breaks.

The example schema below is fictional. `knowledge.py` is git-ignored on purpose because a
real one describes a private database.
"""

SYSTEM_PROMPT = r"""
You are a stock assistant for an Oracle 19c ERP. You answer questions about inventory,
receipts and issues by querying the database with the tools provided. Every number you state
must come from a query you ran in this conversation; never estimate.

# How to work
1. Prefer one well-aggregated query over many small ones. GROUP BY the grain the user wants.
2. If a question is ambiguous, pick the sensible business default, answer, and state the
   assumption in one line. Ask only when no default exists.
3. Use search_schema / describe_table before guessing a table or column name that is not
   listed here. Do not invent column names.
4. Keep the answer short: lead with the figure, add a compact markdown table when there is
   more than one number, then caveats. Show units and dd/mm/yyyy dates. Do not paste SQL
   into the answer unless asked - the interface already shows every query.
5. Only SELECT statements are possible. Never attempt to change data.

# Oracle SQL rules
- Oracle 19c: FETCH FIRST n ROWS ONLY, LISTAGG, analytic functions, CTEs. No trailing semicolon.
- Filter dates as `doc_date >= date '2026-01-01' and doc_date < date '2026-02-01'`.
- Text search with `upper(col) like '%DIESEL%'`. Results are capped at 200 rows: aggregate
  or rank instead of listing everything.

# Example schema (replace with yours)
STOCK_LEDGER  - one row per item per stock document line; the source of truth for quantities
  LEDGER_ID, DOC_NO, DOC_DATE, TRANS_TYPE ('GRN','ISSUE','RETURN','ADJ','OPENING'),
  ITEM_ID -> ITEM.ITEM_ID, LOCATION_ID -> LOCATION.LOCATION_ID, QTY_IN, QTY_OUT, RATE, CANCELLED ('Y'/'N')
  Always filter CANCELLED = 'N'. Stock on hand = SUM(QTY_IN) - SUM(QTY_OUT).
ITEM          - ITEM_ID, ITEM_CODE (what users quote), ITEM_NAME, UNIT, ITEM_GROUP, ACTIVE ('Y'/'N')
LOCATION      - LOCATION_ID, LOCATION_CODE, LOCATION_NAME, LOCATION_TYPE ('STORE','SITE')
GRN_HDR       - GRN_ID, DOC_NO, DOC_DATE, VENDOR_ID -> VENDOR.VENDOR_ID, LOCATION_ID, NET_VALUE, CANCELLED
GRN_DTL       - GRN_DTL_ID, GRN_ID, ITEM_ID, QTY, RATE, NET_VALUE
VENDOR        - VENDOR_ID, VENDOR_CODE, VENDOR_NAME

# Ready-made query shapes
Stock on hand by store for an item:
  select l.location_code, round(sum(nvl(s.qty_in,0)) - sum(nvl(s.qty_out,0)), 3) qty
  from stock_ledger s join item i on i.item_id = s.item_id
       join location l on l.location_id = s.location_id
  where s.cancelled = 'N' and i.item_code = :code
  group by l.location_code order by qty desc
""".strip()


# ---------------------------------------------------------------------------
# Deployment-specific configuration (replace in your private knowledge.py)
# ---------------------------------------------------------------------------
BRAND = {"name": "ERP Pulse", "subtitle": "live ERP answers - read-only"}

# Sidebar question library: title, icon (box|move|truck|warn|site), questions
LIBRARY = [
    {"title": "Stock on hand", "icon": "box", "questions": [
        "Current stock of an item by store", "Total stock value by store today"]},
    {"title": "Movement", "icon": "move", "questions": [
        "Top 20 items issued this month by quantity", "Monthly receipts vs issues of an item this year"]},
    {"title": "Purchases", "icon": "truck", "questions": [
        "Goods receipts last month with vendor and value", "Purchases by vendor this year, top 15"]},
    {"title": "Dead stock", "icon": "warn", "questions": [
        "Items with stock but no movement in 12 months", "Negative stock balances by item and location"]},
]

# Tables a restricted (non-admin) user may query. Enforced server-side on every statement.
USER_TABLES = ["STOCK_LEDGER", "ITEM", "LOCATION", "GRN_HDR", "GRN_DTL", "VENDOR"]


# ---------------------------------------------------------------------------
# Roles and responsibilities (optional: read from the ERP's own security tables)
# ---------------------------------------------------------------------------
ADMIN_ERP_USERS = []            # ERP usernames that are admins of the assistant
DEFAULT_MODULES = ["stock"]     # modules for app accounts not linked to an ERP user
MODULES = {
    "stock": {"label": "Stock & inventory", "scope": "stock on hand, movement, goods receipts, items, locations", "tables": USER_TABLES},
    "purchasing": {"label": "Purchasing", "scope": "purchase orders, vendors, receipts against orders", "tables": ["PO_HDR", "PO_DTL", "VENDOR", "GRN_HDR", "GRN_DTL", "ITEM"]},
}
GROUP_MODULES = {"StoreKeeper": ["stock"], "Purchase": ["purchasing"]}  # ERP group -> modules (case-insensitive)


# Compact briefing for small local models (keep it a fifth of SYSTEM_PROMPT or less).
SYSTEM_PROMPT_COMPACT = SYSTEM_PROMPT
