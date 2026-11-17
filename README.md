# Document Intake Agent

A multi-agent pipeline built with Google's **Agent Development Kit (ADK)** and **Gemini**
that turns invoices and receipts (PDFs, scans and phone photos) into checked, structured
records. It reads the document, validates every value against the company's system of
record through tools, and either accepts it automatically or sends it to a person with
reasons they can act on.

> Status: in progress. Results below are filled in from real eval runs only.

## Why

Finance teams retype invoices into their ERP and catch problems by eye: a total that
does not add up, an invoice already paid, an amount that does not match the purchase
order. The hard part of automating this is not reading the document. It is trusting the
result enough to act on it. So this project measures how often the agent is right, and
above all how often it **wrongly accepts** a document that needed a person.

## How it works

```
document ──> intake_pipeline (ADK Workflow graph)
               1. extractor  Gemini reads the document           -> state["extracted"]
               2. validator  checks it against the ERP via tools -> state["validation"]
                    find_vendor · get_purchase_order · find_payments
                    check_totals · compare_to_po
               3. reviewer   auto_accept or needs_review + reasons -> state["decision"]
```

- **Structured outputs:** the extractor and reviewer are constrained to Pydantic schemas
  (`docintake/schemas.py`).
- **Integration through tools:** every lookup is an HTTP call to a mock ERP
  (`mock_erp/app.py`), as it would be at a real customer. The vendor search is
  deliberately loose, so the agent has to judge matches.
- **Arithmetic by tool, not by the model:** totals and PO tolerances are checked in code.
- **Fail closed:** if a check cannot be done (ERP down), the document goes to a person.

## Data

60 synthetic documents (`python -m docintake.generate`): 36 invoices and 24 receipts across
14 scenarios (clean, already paid, PO amount mismatch, closed PO, wrong vendor's PO,
currency mismatch, totals that do not add up, unknown vendor, missing PO, receipt over
the limit), rendered as PDFs, clean scans and phone photos (tilted, blurred, noisy).
The correct answer for every document comes from the business rules in
`docintake/rules.py`. No real data is used.

## Results

| Metric | Value |
| --- | --- |
| All fields correct | _TBD_ |
| Decision correct | _TBD_ |
| False accepts | _TBD_ |
| Latency median / p90 | _TBD_ |
| Tokens and cost per document | _TBD_ |

_Fill from `results/run-*.md`. Add the before/after of the main fix._

## Run it

Commands use `python -m ...` because Windows Smart App Control can block the `.exe`
launchers pip creates in `.venv\Scripts` (`uvicorn.exe`, `adk.exe`).

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp .env.example .env                              # add a Gemini API key
python -m docintake.generate                      # documents, ERP data, labels
python -m pytest                                  # rules, data, ERP, tools, scoring
python -m uvicorn mock_erp.app:app --port 8001    # in a second terminal
python -m google.adk.cli web agents               # chat UI: upload a document
python -m docintake.eval --limit 10               # measured run
```

## Deployment on Google Cloud

_TBD: Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`), Cloud Run for the agent and the
mock ERP, Secret Manager, Cloud Trace._

## What I would do next

_TBD from the eval failures._
