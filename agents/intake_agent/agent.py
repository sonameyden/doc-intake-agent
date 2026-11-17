"""The document intake pipeline: extract -> validate (tools) -> decide.

    python -m google.adk.cli web agents   # chat UI; upload a document from data/documents/
    python -m docintake.eval --limit 10

Three specialist agents run as an ADK Workflow graph (START -> extractor -> validator ->
reviewer) and hand work over through session state: the extractor writes state["extracted"], the validator reads it
and writes state["validation"], the reviewer reads both and writes state["decision"].
"""

import os

from dotenv import find_dotenv, load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.workflow import START, Workflow

from docintake.schemas import Decision, ExtractedDocument

from . import prompts, tools

load_dotenv(find_dotenv(usecwd=True))

# gemini-2.5-flash is deprecated for new users (Nov 2026); see the AI Studio model list.
MODEL = os.getenv("MODEL", "gemini-3.5-flash-lite")

Model = str | BaseLlm


def build_pipeline(extractor_model: Model = MODEL, validator_model: Model = MODEL, reviewer_model: Model = MODEL) -> Workflow:
    """The pipeline with a model per agent: a Gemini model name, or (in tests) a
    scripted BaseLlm. A stronger model for extraction and a cheaper one for the
    reviewer is a cost experiment worth measuring with the eval."""
    extractor = LlmAgent(
        name="extractor",
        model=extractor_model,
        description="Reads an invoice or receipt and returns its printed fields.",
        instruction=prompts.EXTRACTOR,
        output_schema=ExtractedDocument,
        output_key="extracted",
    )
    validator = LlmAgent(
        name="validator",
        model=validator_model,
        description="Checks the extracted fields against the ERP through tools.",
        instruction=prompts.VALIDATOR,
        tools=[tools.find_vendor, tools.get_purchase_order, tools.find_payments, tools.check_totals, tools.compare_to_po,
               tools.check_receipt_limit],
        output_key="validation",
        # Cost experiment for the eval: the validator only needs state["extracted"], not
        # the document. Try include_contents="none" and compare tokens and accuracy.
    )
    reviewer = LlmAgent(
        name="reviewer",
        model=reviewer_model,
        description="Decides auto_accept or needs_review with reason codes.",
        instruction=prompts.REVIEWER,
        output_schema=Decision,
        output_key="decision",
        # Everything it needs is in state; not re-sending the document saves input tokens.
        include_contents="none",
    )
    return Workflow(
        name="intake_pipeline",
        description="Document intake: extract, validate against the ERP, decide.",
        edges=[(START, extractor, validator, reviewer)],
    )


root_agent = build_pipeline()
