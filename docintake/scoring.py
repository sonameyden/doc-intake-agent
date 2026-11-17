"""Compare the agent's output for one document with its label."""

import re
from decimal import Decimal, InvalidOperation

FIELDS = ("document_type", "vendor_name", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total", "po_number")
AMOUNTS = {"subtotal", "tax", "total"}


def _norm_text(value) -> str | None:
    if value in (None, ""):
        return None
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) or None


def _amount(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def field_matches(name: str, expected, actual) -> bool:
    if name in AMOUNTS:
        e, a = _amount(expected), _amount(actual)
        return e is None and a is None or (e is not None and a is not None and abs(e - a) <= Decimal("0.01"))
    return _norm_text(expected) == _norm_text(actual)


def score(label: dict, extracted: dict | None, decision: dict | None) -> dict:
    extracted = extracted or {}
    decision = decision or {}
    fields = {name: field_matches(name, label["fields"].get(name), extracted.get(name)) for name in FIELDS}
    predicted = decision.get("decision")
    expected_reasons, predicted_reasons = set(label["reasons"]), set(decision.get("reasons") or [])
    return {
        "fields": fields,
        "all_fields": all(fields.values()),
        "decision_correct": predicted == label["decision"],
        "reasons_exact": predicted_reasons == expected_reasons,
        "missed_reasons": sorted(expected_reasons - predicted_reasons),
        "extra_reasons": sorted(predicted_reasons - expected_reasons),
        # The costly error: a document that needed a person was accepted automatically.
        "false_accept": predicted == "auto_accept" and label["decision"] == "needs_review",
        "false_review": predicted == "needs_review" and label["decision"] == "auto_accept",
    }
