"""Tools the validator agent calls. Each ERP tool is an HTTP call to the mock ERP, as an
integration with a customer's real system would be. Tools return plain dicts with a
``status`` so the model can tell "not found" from "the system is down"."""

import os
from decimal import Decimal, InvalidOperation

import httpx

ERP_URL = os.getenv("ERP_URL", "http://127.0.0.1:8001")
_client = httpx.Client(base_url=ERP_URL, timeout=10.0)


def _get(path: str, params: dict | None = None) -> dict:
    try:
        response = _client.get(path, params=params)
    except httpx.HTTPError as error:
        return {"status": "error", "message": f"ERP unreachable: {error.__class__.__name__}"}
    if response.status_code == 404:
        return {"status": "not_found"}
    if response.status_code >= 400:
        return {"status": "error", "message": f"ERP returned {response.status_code}"}
    return {"status": "ok", "data": response.json()}


def find_vendor(name: str) -> dict:
    """Search the ERP's vendor master by name. The search is loose: check that a hit is
    really the same company (its name or one of its aliases) before relying on it.

    Args:
        name: The vendor name as printed on the document.
    """
    return _get("/vendors", {"name": name})


def get_purchase_order(po_number: str) -> dict:
    """Look up a purchase order: its vendor_id, currency, approved amount and status
    ("open" or "closed").

    Args:
        po_number: The purchase order number printed on the invoice, e.g. "PO-4123".
    """
    return _get(f"/purchase-orders/{po_number}")


def find_payments(invoice_number: str, vendor_id: str) -> dict:
    """List payments already made for this invoice or receipt number to this vendor.
    Any hit means the document was already paid.

    Args:
        invoice_number: The invoice or receipt number printed on the document.
        vendor_id: The vendor's id from find_vendor, e.g. "V003".
    """
    return _get("/payments", {"invoice_number": invoice_number, "vendor_id": vendor_id})


def check_totals(subtotal: float, tax: float, total: float) -> dict:
    """Check that subtotal + tax equals the printed total, to the cent. Do this
    arithmetic with the tool, not in your head.

    Args:
        subtotal: Amount before tax.
        tax: Tax amount (0 if none).
        total: Printed total.
    """
    try:
        expected = Decimal(str(subtotal)) + Decimal(str(tax))
        difference = Decimal(str(total)) - expected
    except InvalidOperation:
        return {"status": "error", "message": "Amounts must be numbers."}
    return {
        "status": "ok",
        "adds_up": abs(difference) <= Decimal("0.01"),
        "expected_total": float(expected),
        "difference": float(difference),
    }


RECEIPT_LIMITS = {"USD": Decimal("200"), "INR": Decimal("15000"), "BTN": Decimal("15000")}


def check_receipt_limit(total: float, currency: str) -> dict:
    """Check a receipt's total against the company's limit for receipts paid without a
    purchase order (200 USD, 15000 INR, 15000 BTN). Use this; do not compare amounts
    yourself.

    Args:
        total: The receipt's printed total.
        currency: "USD", "INR" or "BTN".
    """
    limit = RECEIPT_LIMITS.get(str(currency).upper())
    if limit is None:
        return {"status": "error", "message": f"No receipt limit for currency {currency!r}."}
    return {"status": "ok", "over_limit": Decimal(str(total)) > limit, "limit": float(limit), "currency": currency}


def compare_to_po(invoice_total: float, po_amount: float) -> dict:
    """Compare an invoice total with its purchase order amount. Within 1% counts as a
    match (rounding, small fees).

    Args:
        invoice_total: The total printed on the invoice.
        po_amount: The purchase order's approved amount.
    """
    total, approved = Decimal(str(invoice_total)), Decimal(str(po_amount))
    difference = total - approved
    return {
        "status": "ok",
        "within_tolerance": abs(difference) <= approved * Decimal("0.01"),
        "difference": float(difference),
        "difference_percent": float(difference / approved * 100) if approved else None,
    }
