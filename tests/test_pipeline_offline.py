"""End-to-end wiring test with scripted models instead of Gemini: the Workflow graph,
state handover between agents, real tool calls against the mock ERP, and the eval
runner's parsing. No API key or network needed."""

import asyncio
import json
import sys
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from docintake import LABELS_FILE, ROOT


class ScriptedModel(BaseLlm):
    """Returns the given contents in order, one per model call, and records requests."""

    model: str = "scripted"
    script: list[types.Content] = []
    requests: list[LlmRequest] = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        content = self.script[len(self.requests) - 1]
        yield LlmResponse(content=content, usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=10))


def text(value: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=value)])


def call(tool: str, **args) -> types.Content:
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=tool, args=args))])


@pytest.fixture
def pipeline(monkeypatch):
    if not LABELS_FILE.exists():
        pytest.skip("run: python -m docintake.generate")
    sys.path.insert(0, str(ROOT / "agents"))
    from mock_erp.app import app
    from intake_agent import agent, tools

    monkeypatch.setattr(tools, "_client", TestClient(app))
    return agent


@pytest.mark.parametrize("validator_context", ["default", "none"])
def test_pipeline_runs_end_to_end_with_scripted_models(pipeline, monkeypatch, validator_context):
    from google.adk.runners import InMemoryRunner

    from docintake.eval import APP, run_one
    from docintake.scoring import score

    label = next(json.loads(line) for line in LABELS_FILE.read_text(encoding="utf-8").splitlines()
                 if json.loads(line)["scenario"] == "already_paid")
    f = label["fields"]
    extracted = {**f, "subtotal": float(f["subtotal"]), "tax": float(f["tax"]), "total": float(f["total"])}

    models = {
        "extractor": ScriptedModel(script=[text(json.dumps(extracted))], requests=[]),
        "validator": ScriptedModel(script=[
            call("find_vendor", name=f["vendor_name"]),
            call("check_totals", subtotal=extracted["subtotal"], tax=extracted["tax"], total=extracted["total"]),
            text("Vendor found. Totals add up. A payment already exists for this invoice."),
        ], requests=[]),
        "reviewer": ScriptedModel(script=[text(json.dumps(
            {"decision": "needs_review", "reasons": ["already_paid"], "summary": "Already paid."}))], requests=[]),
    }
    workflow = pipeline.build_pipeline(models["extractor"], models["validator"], models["reviewer"], validator_context)
    runner = InMemoryRunner(agent=workflow, app_name=APP)
    result = asyncio.run(run_one(runner, label))

    # State handed over from agent to agent.
    assert result["extracted"]["invoice_number"] == f["invoice_number"]
    assert result["validation"].startswith("Vendor found")
    assert result["decision"]["decision"] == "needs_review"
    # Tools were really called, against the mock ERP.
    assert result["tool_calls"] == ["find_vendor", "check_totals"]
    tool_results = [part.function_response.response for content in models["validator"].requests[-1].contents
                    for part in content.parts or [] if part.function_response]
    assert tool_results[0]["status"] == "ok" and tool_results[0]["data"]["vendors"]
    assert tool_results[1]["adds_up"] is True
    # {extracted} was filled into the validator's instructions from state.
    assert f["invoice_number"] in str(models["validator"].requests[0].config.system_instruction)
    # The reviewer did not receive the document again (include_contents="none").
    reviewer_parts = [p for c in models["reviewer"].requests[0].contents for p in c.parts or []]
    assert not any(p.inline_data for p in reviewer_parts)
    # The latency experiment: with "none" the validator no longer receives the document
    # image, but it still sees its own tool calls and results within its turn.
    validator_saw_document = any(p.inline_data for c in models["validator"].requests[0].contents for p in c.parts or [])
    assert validator_saw_document is (validator_context == "default")
    assert [r["status"] for r in tool_results] == ["ok", "ok"]
    # Usage is counted, and the result scores as the label expects.
    assert result["tokens_in"] == 500 and result["tokens_out"] == 50
    scored = score(label, result["extracted"], result["decision"])
    assert scored["all_fields"] and scored["decision_correct"]


def test_guardrail_catches_a_reviewer_that_accepts_a_closed_po(pipeline):
    """Replay of 17 Nov, document 030: the validator reported the PO as closed and the
    reviewer still accepted it. The guardrail must turn that into needs_review."""
    from google.adk.runners import InMemoryRunner

    from docintake.eval import APP, run_one
    from docintake.scoring import score

    label = next(json.loads(line) for line in LABELS_FILE.read_text(encoding="utf-8").splitlines()
                 if json.loads(line)["file"].startswith("030"))
    assert label["reasons"] == ["po_closed"]
    f = label["fields"]
    extracted = {**f, "subtotal": float(f["subtotal"]), "tax": float(f["tax"]), "total": float(f["total"])}
    erp = json.loads((ROOT / "data" / "erp.json").read_text(encoding="utf-8"))
    vendor_id = next(v["id"] for v in erp["vendors"] if f["vendor_name"] in [v["name"], *v["aliases"]])

    models = [
        ScriptedModel(script=[text(json.dumps(extracted))], requests=[]),
        ScriptedModel(script=[
            call("find_vendor", name=f["vendor_name"]),
            call("check_totals", subtotal=extracted["subtotal"], tax=extracted["tax"], total=extracted["total"]),
            call("get_purchase_order", po_number=f["po_number"]),
            call("find_payments", invoice_number=f["invoice_number"], vendor_id=vendor_id),
            text("Purchase Order: Found, matches vendor, status is closed, total matches."),
        ], requests=[]),
        ScriptedModel(script=[text(json.dumps(
            {"decision": "auto_accept", "reasons": [], "summary": "Matches the PO perfectly."}))], requests=[]),
    ]
    runner = InMemoryRunner(agent=pipeline.build_pipeline(*models), app_name=APP)
    result = asyncio.run(run_one(runner, label))

    assert result["decision_llm"]["decision"] == "auto_accept"          # the reviewer's mistake
    assert result["decision"]["decision"] == "needs_review"             # what the pipeline returns
    assert result["decision"]["reasons"] == ["po_closed"]
    assert result["guardrail"] == {"overrode_accept": True, "added_reasons": ["po_closed"], "gaps": []}
    assert score(label, result["extracted"], result["decision"])["decision_correct"]
    assert score(label, result["extracted"], result["decision_llm"])["false_accept"]


def test_guardrail_fails_closed_when_a_lookup_was_skipped(pipeline):
    from intake_agent.guardrail import enforce

    extracted = {"document_type": "receipt", "vendor_name": "Druk Office Supplies", "invoice_number": "R1",
                 "currency": "BTN", "subtotal": 100, "tax": 0, "total": 100, "po_number": None}
    facts = {"vendor": {"query": "Druk Office Supplies", "status": "ok",
                        "data": {"vendors": [{"id": "V001", "name": "Druk Office Supplies", "aliases": []}]}}}
    final, record = enforce(extracted, {"decision": "auto_accept", "reasons": []}, facts)  # payments never checked
    assert final["decision"] == "needs_review" and record["overrode_accept"] and record["gaps"]
    # A lookup with a different invoice number does not count either.
    facts["payments"] = {"query": {"invoice_number": "R999", "vendor_id": "V001"}, "status": "ok", "data": {"payments": []}}
    assert enforce(extracted, {"decision": "auto_accept", "reasons": []}, facts)[0]["decision"] == "needs_review"
    facts["payments"]["query"]["invoice_number"] = "R1"
    assert enforce(extracted, {"decision": "auto_accept", "reasons": []}, facts)[0]["decision"] == "auto_accept"


def test_guardrail_agrees_with_the_business_rules_on_every_document(pipeline):
    """With correct lookups, the evidence the guardrail derives equals the labels."""
    from intake_agent.guardrail import evidence_reasons

    erp = json.loads((ROOT / "data" / "erp.json").read_text(encoding="utf-8"))
    for line in LABELS_FILE.read_text(encoding="utf-8").splitlines():
        label = json.loads(line)
        f = label["fields"]
        hits = [v for v in erp["vendors"] if f["vendor_name"].lower() in [n.lower() for n in [v["name"], *v["aliases"]]]]
        facts = {"vendor": {"query": f["vendor_name"], "status": "ok", "data": {"vendors": hits}}}
        if hits:
            paid = [p for p in erp["payments"] if p["invoice_number"] == f["invoice_number"] and p["vendor_id"] == hits[0]["id"]]
            facts["payments"] = {"query": {"invoice_number": f["invoice_number"], "vendor_id": hits[0]["id"]},
                                 "status": "ok", "data": {"payments": paid}}
        if f["po_number"]:
            po = next((p for p in erp["purchase_orders"] if p["po_number"] == f["po_number"]), None)
            facts["purchase_order"] = {"query": f["po_number"], "status": "ok", "data": po} if po else \
                {"query": f["po_number"], "status": "not_found"}
        reasons, gaps = evidence_reasons(f, facts)
        assert (sorted(reasons), gaps) == (sorted(label["reasons"]), []), label["file"]
