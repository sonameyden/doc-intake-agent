"""The last step of the pipeline: the model proposes, code enforces.

The reviewer is an LLM and is usually right. But on 17 Nov it accepted an invoice whose
purchase order the validator had just reported as *closed*. So the final decision is
checked against the evidence:

- ERP facts are what the tools actually returned (``state["facts"]``), and each lookup
  must have used the extracted reference (invoice number, PO number). A missing,
  mismatched or failed lookup sends the document to a person (fail closed).
- Arithmetic is redone here from the extracted fields: totals, the 1% PO tolerance and
  the receipt limits. Numbers the model passed to tools are not trusted.
- Reasons are only ever added, never removed, so the reviewer's judgment calls stand.

The reviewer's own answer is kept in ``state["decision_llm"]`` so the eval can report how
the reviewer did alone and how often the guardrail stepped in.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from docintake.rules import PO_TOLERANCE, REASONS, RECEIPT_LIMITS

CENT = Decimal("0.01")


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value) if isinstance(value, dict) else {}


def _amount(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def _same(a: Any, b: Any) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def evidence_reasons(extracted: dict, facts: dict) -> tuple[set[str], list[str]]:
    """Reasons the evidence proves, and checks that could not be confirmed."""
    reasons: set[str] = set()
    gaps: list[str] = []
    doc_type = extracted.get("document_type")
    currency = extracted.get("currency")
    subtotal, tax, total = (_amount(extracted.get(k)) for k in ("subtotal", "tax", "total"))

    if None in (subtotal, tax, total):
        gaps.append("amounts were not read, so totals could not be checked")
    elif (subtotal + tax - total).copy_abs() > CENT:
        reasons.add("totals_mismatch")

    vendor = facts.get("vendor")
    vendor_id = None
    if not vendor or vendor.get("status") != "ok":
        gaps.append("the vendor was not looked up in the ERP")
    else:
        name = extracted.get("vendor_name")
        exact = [v for v in vendor["data"].get("vendors", []) if any(_same(name, n) for n in [v["name"], *v.get("aliases", [])])]
        if exact:
            vendor_id = exact[0]["id"]
        else:
            reasons.add("unknown_vendor")

    if vendor_id is not None:
        payments = facts.get("payments")
        query = (payments or {}).get("query", {})
        if (not payments or payments.get("status") != "ok"
                or not _same(query.get("invoice_number"), extracted.get("invoice_number"))
                or not _same(query.get("vendor_id"), vendor_id)):
            gaps.append("previous payments were not checked for this invoice and vendor")
        elif payments["data"].get("payments"):
            reasons.add("already_paid")

    if doc_type == "invoice":
        po_number = extracted.get("po_number")
        if not po_number:
            reasons.add("missing_po")
        else:
            po = facts.get("purchase_order")
            if not po or not _same(po.get("query"), po_number) or po.get("status") not in ("ok", "not_found"):
                gaps.append(f"purchase order {po_number} was not looked up")
            elif po["status"] == "not_found":
                reasons.add("po_not_found")
            else:
                record = po["data"]
                if vendor_id is not None and record.get("vendor_id") != vendor_id:
                    reasons.add("po_vendor_mismatch")
                if record.get("status") != "open":
                    reasons.add("po_closed")
                if record.get("currency") != currency:
                    reasons.add("currency_mismatch")
                elif total is not None:
                    approved = Decimal(str(record["amount"]))
                    if (total - approved).copy_abs() > approved * PO_TOLERANCE:
                        reasons.add("po_amount_mismatch")
    elif doc_type == "receipt":
        limit = RECEIPT_LIMITS.get(str(currency).upper())
        if limit is None or total is None:
            gaps.append("the receipt limit could not be checked")
        elif total > limit:
            reasons.add("over_receipt_limit")
    else:
        gaps.append("the document type was not read")
    return reasons, gaps


def enforce(extracted: dict, decision: dict, facts: dict) -> tuple[dict, dict]:
    """The final decision and a record of what the guardrail did."""
    proposed = {"decision": decision.get("decision"), "reasons": list(decision.get("reasons") or []),
                "summary": decision.get("summary") or ""}
    proven, gaps = evidence_reasons(extracted, facts)
    reasons = set(proposed["reasons"]) | proven
    final_decision = "needs_review" if reasons or gaps or proposed["decision"] != "auto_accept" else "auto_accept"
    added = sorted(proven - set(proposed["reasons"]), key=REASONS.index)
    final = {
        "decision": final_decision,
        "reasons": [r for r in REASONS if r in reasons],
        "summary": proposed["summary"],
    }
    notes = []
    if added:
        notes.append("Guardrail added from ERP evidence: " + ", ".join(added) + ".")
    if gaps:
        notes.append("Not confirmed: " + "; ".join(gaps) + ".")
    if notes:
        final["summary"] = (final["summary"] + " " + " ".join(notes)).strip()
    record = {
        "overrode_accept": proposed["decision"] == "auto_accept" and final_decision == "needs_review",
        "added_reasons": added,
        "gaps": gaps,
    }
    return final, record


def enforce_evidence(ctx) -> None:
    """Workflow step after the reviewer (ADK passes the node context as ``ctx``)."""
    decision = _as_dict(ctx.state.get("decision"))
    final, record = enforce(_as_dict(ctx.state.get("extracted")), decision, dict(ctx.state.get("facts") or {}))
    ctx.state["decision_llm"] = decision
    ctx.state["decision"] = final
    ctx.state["guardrail"] = record
