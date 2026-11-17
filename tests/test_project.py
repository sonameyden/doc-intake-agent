"""Checks that need no model: the rules, the generated data, the mock ERP, the tools and
the scoring. Run with: pytest"""

import json
import sys
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from docintake import DOCUMENTS, ERP_FILE, LABELS_FILE, ROOT
from docintake.rules import Fields, decide
from docintake.scoring import score

ERP = {
    "vendors": [{"id": "V1", "name": "Druk Office Supplies", "aliases": ["Druk Office Supplies Pvt. Ltd."]}],
    "purchase_orders": [
        {"po_number": "PO-1", "vendor_id": "V1", "currency": "BTN", "amount": "1050.00", "status": "open"},
        {"po_number": "PO-2", "vendor_id": "V9", "currency": "BTN", "amount": "1050.00", "status": "closed"},
    ],
    "payments": [{"invoice_number": "INV-9", "vendor_id": "V1"}],
}


def invoice(**changes) -> Fields:
    base = dict(document_type="invoice", vendor_name="Druk Office Supplies", invoice_number="INV-1",
                invoice_date="2026-10-01", currency="BTN", subtotal=Decimal("1000.00"), tax=Decimal("50.00"),
                total=Decimal("1050.00"), po_number="PO-1")
    return Fields(**{**base, **changes})


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ("auto_accept", [])),
        ({"vendor_name": "Druk Office Supplies Pvt. Ltd."}, ("auto_accept", [])),
        ({"total": Decimal("1060.00")}, ("needs_review", ["totals_mismatch"])),
        ({"total": Decimal("1200.00"), "subtotal": Decimal("1150.00")}, ("needs_review", ["po_amount_mismatch"])),
        ({"invoice_number": "INV-9"}, ("needs_review", ["already_paid"])),
        ({"po_number": None}, ("needs_review", ["missing_po"])),
        ({"po_number": "PO-404"}, ("needs_review", ["po_not_found"])),
        ({"po_number": "PO-2"}, ("needs_review", ["po_vendor_mismatch", "po_closed"])),
        ({"currency": "INR"}, ("needs_review", ["currency_mismatch"])),
        ({"vendor_name": "Norbu Motors"}, ("needs_review", ["unknown_vendor"])),
        ({"document_type": "receipt", "po_number": None, "total": Decimal("16050.00"), "subtotal": Decimal("16000.00")},
         ("needs_review", ["over_receipt_limit"])),
    ],
)
def test_rules(changes, expected):
    assert decide(invoice(**changes), ERP) == expected


def test_one_percent_po_tolerance():
    assert decide(invoice(subtotal=Decimal("1010.00"), total=Decimal("1060.00")), ERP) == ("auto_accept", [])


@pytest.fixture(scope="module")
def labels() -> list[dict]:
    if not LABELS_FILE.exists():
        pytest.skip("run: python -m docintake.generate")
    return [json.loads(line) for line in LABELS_FILE.read_text(encoding="utf-8").splitlines()]


def test_generated_set_is_complete_and_consistent(labels):
    erp = json.loads(ERP_FILE.read_text(encoding="utf-8"))
    assert len(labels) == 60
    assert {label["format"] for label in labels} == {"pdf", "scan", "photo"}
    for label in labels:
        assert (DOCUMENTS / label["file"]).exists()
        f = label["fields"]
        fields = Fields(f["document_type"], f["vendor_name"], f["invoice_number"], f["invoice_date"], f["currency"],
                        Decimal(f["subtotal"]), Decimal(f["tax"]), Decimal(f["total"]), f["po_number"])
        assert decide(fields, erp) == (label["decision"], label["reasons"])
        # Each scenario produces the reason it is named after.
        if label["scenario"] not in ("clean", "wrong_vendor_po"):
            assert label["scenario"] in label["reasons"]
        elif label["scenario"] == "clean":
            assert label["decision"] == "auto_accept"
    assert 0.3 < sum(label["decision"] == "auto_accept" for label in labels) / len(labels) < 0.6


def test_mock_erp(labels):
    from mock_erp.app import app

    client = TestClient(app)
    assert client.get("/vendors", params={"name": "druk office"}).json()["vendors"][0]["id"] == "V001"
    assert client.get("/vendors", params={"name": "Norbu Motors"}).json()["vendors"] == []
    assert client.get("/purchase-orders/PO-0000").status_code == 404
    po = next(l for l in labels if l["scenario"] == "clean" and l["fields"]["po_number"])["fields"]["po_number"]
    assert client.get(f"/purchase-orders/{po}").json()["status"] == "open"


def test_tools_against_mock_erp(labels, monkeypatch):
    from mock_erp.app import app

    sys.path.insert(0, str(ROOT / "agents"))
    from intake_agent import tools

    # TestClient is an httpx.Client that calls the app in-process.
    monkeypatch.setattr(tools, "_client", TestClient(app))
    assert tools.find_vendor("Kolkata Paper Mills Ltd")["data"]["vendors"][0]["id"] == "V006"
    assert tools.get_purchase_order("PO-0000") == {"status": "not_found"}
    paid = next(l for l in labels if l["scenario"] == "already_paid")
    vendor = tools.find_vendor(paid["fields"]["vendor_name"])["data"]["vendors"][0]["id"]
    assert tools.find_payments(paid["fields"]["invoice_number"], vendor)["data"]["payments"]
    assert tools.check_totals(1000, 50, 1050)["adds_up"] is True
    assert tools.check_totals(1000, 50, 1150)["adds_up"] is False
    assert tools.compare_to_po(1060, 1050)["within_tolerance"] is True
    assert tools.compare_to_po(1200, 1050)["within_tolerance"] is False


def test_tools_report_an_unreachable_erp(monkeypatch):
    sys.path.insert(0, str(ROOT / "agents"))
    from intake_agent import tools

    monkeypatch.setattr(tools, "_client", httpx.Client(base_url="http://127.0.0.1:9", timeout=0.5))
    assert tools.find_vendor("Druk")["status"] == "error"


def test_scoring():
    label = {"fields": {"document_type": "invoice", "vendor_name": "Druk Office Supplies", "invoice_number": "DOS-2026-0001",
                        "invoice_date": "2026-10-01", "currency": "BTN", "subtotal": "1000.00", "tax": "50.00",
                        "total": "1050.00", "po_number": "PO-1"},
             "decision": "needs_review", "reasons": ["already_paid"]}
    extracted = {**label["fields"], "vendor_name": "DRUK OFFICE SUPPLIES", "total": 1050.0, "invoice_number": "DOS 2026 0001"}
    result = score(label, extracted, {"decision": "auto_accept", "reasons": []})
    assert result["all_fields"] is True
    assert result["false_accept"] is True
    assert result["missed_reasons"] == ["already_paid"]


def test_schema_tolerates_sloppy_model_output_but_still_requires_every_field():
    from docintake.schemas import Decision, ExtractedDocument

    # What gemini-3.5-flash-lite returned on 17 Nov: amounts left out, "null" as text.
    doc = ExtractedDocument.model_validate({"document_type": "invoice", "vendor_name": "Phuentsholing Logistics",
                                            "invoice_number": "PL-2026-0027", "invoice_date": "2026-10-21",
                                            "currency": "INR", "po_number": "null"})
    assert (doc.subtotal, doc.tax, doc.total, doc.po_number) == (None, None, None, None)
    amounts = ExtractedDocument.model_validate({"document_type": "receipt", "vendor_name": "X", "subtotal": "Nu. 1,234.50",
                                                "tax": "Rs.0", "total": "$1,234.50", "currency": "N/A"})
    assert (amounts.subtotal, amounts.tax, amounts.total, amounts.currency) == (1234.5, 0.0, 1234.5, None)
    # The model is still asked for all of them.
    assert set(ExtractedDocument.model_json_schema()["required"]) == set(ExtractedDocument.model_fields)
    assert Decision.model_validate({"decision": "auto_accept"}).reasons == []


def test_eval_retries_an_overloaded_model_and_gives_up_on_real_errors(monkeypatch):
    import asyncio

    from docintake import eval as ev

    busy = RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
    wrapped = RuntimeError("Dynamic node intake_pipeline failed")
    wrapped.__cause__ = busy  # ADK wraps the provider's error, as on 17 Nov
    assert ev.is_transient(wrapped)
    assert not ev.is_transient(ValueError("No API key was provided"))

    calls = []

    async def flaky(runner, label):
        calls.append(1)
        if len(calls) < 3:
            raise wrapped
        return {"decision": {"decision": "auto_accept"}}

    async def no_wait(seconds):
        return None

    monkeypatch.setattr(ev, "run_one", flaky)
    monkeypatch.setattr(ev.asyncio, "sleep", no_wait)
    result = asyncio.run(ev.run_with_retry(None, {}))
    assert result["attempts"] == 3 and "error" not in result

    async def broken(runner, label):
        raise ValueError("No API key was provided")

    monkeypatch.setattr(ev, "run_one", broken)
    assert asyncio.run(ev.run_with_retry(None, {}))["attempts"] == 1


def test_receipt_limit_tool_compares_amounts_so_the_model_does_not(monkeypatch):
    sys.path.insert(0, str(ROOT / "agents"))
    from intake_agent import tools

    # 17 Nov: the reviewer said a Nu. 1,627.50 receipt "exceeds the limit of 15000".
    assert tools.check_receipt_limit(1627.5, "BTN")["over_limit"] is False
    assert tools.check_receipt_limit(19500, "BTN")["over_limit"] is True
    assert tools.check_receipt_limit(200, "USD")["over_limit"] is False
    assert tools.check_receipt_limit(200.01, "usd")["over_limit"] is True
    assert tools.check_receipt_limit(10, "EUR")["status"] == "error"


def test_eval_can_select_files():
    from docintake.eval import load_labels

    if not LABELS_FILE.exists():
        pytest.skip("run: python -m docintake.generate")
    expected = ["016", "047", "048"]
    assert [label["file"][:3] for label in load_labels(None, None, "016, 047,048")] == expected
    # What PowerShell passes for an unquoted 016,047,048: the numbers, zeros dropped.
    assert [label["file"][:3] for label in load_labels(None, None, ["16", "47", "48"])] == expected
    assert [label["file"][:3] for label in load_labels(None, None, ["16,47,48"])] == expected
    with pytest.raises(SystemExit):
        load_labels(None, None, ["abc"])


def test_eval_offset_selects_the_second_half():
    from docintake.eval import load_labels

    if not LABELS_FILE.exists():
        pytest.skip("run: python -m docintake.generate")
    second = load_labels(None, None, offset=30)
    assert len(second) == 30 and second[0]["file"].startswith("031")
    assert load_labels(30, None)[-1]["file"].startswith("030")


def test_summary_merges_runs_with_the_later_run_winning(tmp_path, capsys, monkeypatch):
    from docintake import summary

    base = {"config": {"model": "m", "validator_context": "none"}, "format": "pdf", "hard": False, "scenario": "clean",
            "expected": "auto_accept", "expected_reasons": [], "latency_s": 1.0, "tokens_in": 10, "tokens_out": 1}
    from docintake.scoring import FIELDS

    ok_score = {"fields": {name: True for name in FIELDS}, "all_fields": True, "decision_correct": True, "reasons_exact": True, "missed_reasons": [],
                "extra_reasons": [], "false_accept": False, "false_review": False}
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text(json.dumps({**base, "file": "001.pdf", "score": ok_score}) + "\n"
                     + json.dumps({**base, "file": "002.pdf", "error": "429"}) + "\n", encoding="utf-8")
    second.write_text(json.dumps({**base, "file": "002.pdf", "score": ok_score}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["summary", str(first), str(second)])
    summary.main()
    out = capsys.readouterr().out
    assert "60 documents" not in out and "Decision correct | 2/2" in out and "Errors (no result) | 0" in out

    mixed = tmp_path / "c.jsonl"
    mixed.write_text(json.dumps({**base, "config": {"model": "other"}, "file": "003.pdf", "score": ok_score}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["summary", str(first), str(mixed)])
    with pytest.raises(SystemExit):
        summary.main()
