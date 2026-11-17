"""Business rules for document intake: the known-correct answer for every test document.

The generator uses these rules to label each synthetic document, and the eval compares
the agent's decision against them. The agent never imports this module: it has to reach
the same decision from the document and the ERP tools alone.
"""

from dataclasses import dataclass
from decimal import Decimal

# Receipts need no purchase order, but above these amounts a person must approve them.
RECEIPT_LIMITS: dict[str, Decimal] = {
    "USD": Decimal("200"),
    "INR": Decimal("15000"),
    "BTN": Decimal("15000"),
}
# An invoice may differ from its purchase order by this fraction (rounding, small fees).
PO_TOLERANCE = Decimal("0.01")
CENT = Decimal("0.01")

REASONS = (
    "unknown_vendor",
    "missing_po",
    "po_not_found",
    "po_vendor_mismatch",
    "po_closed",
    "currency_mismatch",
    "po_amount_mismatch",
    "already_paid",
    "totals_mismatch",
    "over_receipt_limit",
)


@dataclass(frozen=True)
class Fields:
    document_type: str  # "invoice" | "receipt"
    vendor_name: str
    invoice_number: str
    invoice_date: str  # YYYY-MM-DD
    currency: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    po_number: str | None


def find_vendor(name: str, erp: dict) -> dict | None:
    wanted = name.strip().lower()
    for vendor in erp["vendors"]:
        names = [vendor["name"], *vendor.get("aliases", [])]
        if any(n.lower() == wanted for n in names):
            return vendor
    return None


def decide(fields: Fields, erp: dict) -> tuple[str, list[str]]:
    """``("auto_accept", [])`` or ``("needs_review", reasons)``, reasons in REASONS order."""
    reasons: set[str] = set()
    vendor = find_vendor(fields.vendor_name, erp)
    if vendor is None:
        reasons.add("unknown_vendor")

    if (fields.subtotal + fields.tax - fields.total).copy_abs() > CENT:
        reasons.add("totals_mismatch")

    if vendor is not None and any(
        p["invoice_number"] == fields.invoice_number and p["vendor_id"] == vendor["id"]
        for p in erp["payments"]
    ):
        reasons.add("already_paid")

    if fields.document_type == "invoice":
        if not fields.po_number:
            reasons.add("missing_po")
        else:
            po = next((p for p in erp["purchase_orders"] if p["po_number"] == fields.po_number), None)
            if po is None:
                reasons.add("po_not_found")
            else:
                if vendor is not None and po["vendor_id"] != vendor["id"]:
                    reasons.add("po_vendor_mismatch")
                if po["status"] != "open":
                    reasons.add("po_closed")
                if po["currency"] != fields.currency:
                    reasons.add("currency_mismatch")
                elif (fields.total - Decimal(po["amount"])).copy_abs() > Decimal(po["amount"]) * PO_TOLERANCE:
                    reasons.add("po_amount_mismatch")
    else:
        limit = RECEIPT_LIMITS.get(fields.currency)
        if limit is not None and fields.total > limit:
            reasons.add("over_receipt_limit")

    ordered = [r for r in REASONS if r in reasons]
    return ("needs_review", ordered) if ordered else ("auto_accept", [])
