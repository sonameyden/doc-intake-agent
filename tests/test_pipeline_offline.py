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


def test_pipeline_runs_end_to_end_with_scripted_models(pipeline, monkeypatch):
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
    workflow = pipeline.build_pipeline(models["extractor"], models["validator"], models["reviewer"])
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
    # Usage is counted, and the result scores as the label expects.
    assert result["tokens_in"] == 500 and result["tokens_out"] == 50
    scored = score(label, result["extracted"], result["decision"])
    assert scored["all_fields"] and scored["decision_correct"]
