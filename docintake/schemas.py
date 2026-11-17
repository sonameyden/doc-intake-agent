"""Structured outputs the agents must return. Gemini is constrained to these schemas.

Every field stays *required* in the JSON schema, so the model is asked for all of them.
But a model can still drop a key or write "null" as text; the ``before`` validators turn
that into ``None`` (and "Nu. 1,234.50" into 1234.5) instead of failing the whole
pipeline. A missing value then shows up in the eval as a wrong field, where it belongs.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Reason = Literal[
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
]

_NULLS = {"", "null", "none", "n/a", "na", "-"}


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _NULLS:
        return None
    return value


class ExtractedDocument(BaseModel):
    document_type: Literal["invoice", "receipt"]
    vendor_name: str = Field(description="The seller's name exactly as printed.")
    invoice_number: str | None = Field(description="Invoice, bill or receipt number as printed.")
    invoice_date: str | None = Field(description="Issue date as YYYY-MM-DD.")
    currency: Literal["BTN", "INR", "USD"] | None = Field(
        description="Nu./Ngultrum = BTN, Rs./Rupees = INR, $ = USD."
    )
    subtotal: float | None = Field(description="Amount before tax, as printed. A number, e.g. 9150.00.")
    tax: float | None = Field(description="Tax amount, as printed; 0 if none is shown. A number.")
    total: float | None = Field(description="Amount due, as printed. A number, e.g. 9150.00.")
    po_number: str | None = Field(description="Purchase order number, if printed; otherwise null.")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            optional = ("invoice_number", "invoice_date", "currency", "subtotal", "tax", "total", "po_number")
            data = {**{key: None for key in optional}, **data}
        return data

    @field_validator("invoice_number", "invoice_date", "currency", "po_number", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Any:
        value = _blank_to_none(value)
        if isinstance(value, str):
            # "Nu. 1,234.50", "Rs.9150", "$85.00" -> 1234.5, 9150.0, 85.0
            cleaned = re.sub(r"[^0-9.\-]", "", value.replace(",", "")).strip(".")
            return float(cleaned) if cleaned else None
        return value


class Decision(BaseModel):
    decision: Literal["auto_accept", "needs_review"]
    reasons: list[Reason] = Field(description="Empty when auto_accept.")
    summary: str = Field(description="One or two sentences a finance clerk can act on.")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {"reasons": [], "summary": "", **data}
        return data
