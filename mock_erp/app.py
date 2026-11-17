"""A mock ERP: the customer's system of record the agent has to integrate with.

    python -m uvicorn mock_erp.app:app --port 8001

Read-only, synthetic data from ``data/erp.json``. Vendor search is deliberately loose
(case-insensitive, aliases, partial names), as a real system's search often is: the
agent has to judge whether a hit is really the same vendor.
"""

import json
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query

from docintake import ERP_FILE

app = FastAPI(title="Mock ERP", description="Synthetic vendors, purchase orders and payments.")


@lru_cache
def erp() -> dict:
    return json.loads(ERP_FILE.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/vendors")
def search_vendors(name: str = Query(min_length=2)) -> dict:
    wanted = name.strip().lower()
    hits = [
        v for v in erp()["vendors"]
        if any(wanted in n.lower() or n.lower() in wanted for n in [v["name"], *v["aliases"]])
    ]
    return {"query": name, "vendors": hits}


@app.get("/purchase-orders/{po_number}")
def get_purchase_order(po_number: str) -> dict:
    for po in erp()["purchase_orders"]:
        if po["po_number"].lower() == po_number.strip().lower():
            return po
    raise HTTPException(status_code=404, detail=f"No purchase order {po_number}")


@app.get("/payments")
def search_payments(invoice_number: str, vendor_id: str | None = None) -> dict:
    hits = [
        p for p in erp()["payments"]
        if p["invoice_number"].lower() == invoice_number.strip().lower()
        and (vendor_id is None or p["vendor_id"] == vendor_id)
    ]
    return {"invoice_number": invoice_number, "payments": hits}
