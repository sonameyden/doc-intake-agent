# Document Intake Agent

A multi-agent pipeline built with Google's **Agent Development Kit (ADK)** and **Gemini**
that turns invoices and receipts (PDFs, scans and phone photos) into checked, structured
records. It reads each document, validates every value against the company's system of
record through tools, and either accepts it automatically or sends it to a person with
reasons they can act on. A code guardrail makes sure the model can never accept a
document against the evidence.

**Final eval, 60 documents: 60/60 fields, 60/60 decisions, 0 false accepts**, at a median
10.3 s and about 6,200 input tokens per document (Gemini 3.5 Flash-Lite).

## Why

Finance teams retype invoices into their ERP and catch problems by eye: a total that
does not add up, an invoice already paid, an amount that does not match the purchase
order. Reading the document is not the hard part of automating this. Trusting the result
enough to act on it is. So this project measures how often the agent is right and, above
all, how often it **wrongly accepts** a document that needed a person.

## How it works

```
document ──> ADK Workflow
               1. extractor         Gemini reads the document, structured output   -> state["extracted"]
               2. validator         checks it against the ERP through tools         -> state["validation"]
                    find_vendor · get_purchase_order · find_payments
                    check_totals · compare_to_po · check_receipt_limit
               3. reviewer          auto_accept or needs_review + reason codes      -> state["decision"]
               4. enforce_evidence  code guardrail: the model proposes, code enforces
```

- **Structured outputs:** the extractor and reviewer return Pydantic schemas
  (`docintake/schemas.py`). Every field is required, but a model that drops a key or
  writes "null" as text does not crash the pipeline; the value is recorded as missing and
  the eval counts it as wrong.
- **Integration through tools:** every lookup is an HTTP call to a mock ERP
  (`mock_erp/app.py`), as it would be at a real customer. The vendor search is loose on
  purpose, so matches have to be judged.
- **Arithmetic in code, not in the model:** totals, the 1% purchase-order tolerance and
  receipt limits are computed by tools.
- **Guardrail** (`agents/intake_agent/guardrail.py`): tools record what the ERP returned;
  after the reviewer, code checks that each lookup used the extracted references, redoes
  the arithmetic, adds any reason the evidence proves, and sends the document to a person
  if a required check is missing or failed. It never removes a reason. The reviewer's own
  answer is kept, so the eval reports both.
- **Each agent gets only what it needs:** the validator and reviewer work from the
  extracted fields in session state, not the image.
- **Transient provider errors** (429, 500, 503, 504) are retried with exponential backoff.

## Results

Final configuration, all 60 documents (three runs merged with `python -m docintake.summary`):

| Metric | Value |
| --- | --- |
| All fields correct | 60/60 |
| Decision correct | 60/60 |
| Reason codes exactly right | 60/60 |
| **False accepts** (needed a person, auto-accepted) | **0** |
| False reviews (fine, sent to a person) | 0 |
| Errors | 0 |
| Latency, median / p90 | 10.3 s / 16.0 s |
| Tokens per document, in / out | 6,215 / 391 |

| Format | Documents | All fields | Decision |
| --- | --- | --- | --- |
| PDF | 20 | 20 | 20 |
| Scan (PNG) | 15 | 15 | 15 |
| Phone photo (JPG) | 18 | 18 | 18 |
| Phone photo, hard (tilted, blurred, low contrast) | 7 | 7 | 7 |

## What went wrong, and what I changed

The numbers above are the end state. Getting there:

| Found | Evidence | Change |
| --- | --- | --- |
| The model left out the amounts and wrote `"null"` as text; the strict schema crashed the pipeline. | First upload in `adk web`. | Fields stay required in the schema; missing keys and text nulls become `None`, and the eval scores them as wrong. |
| The reviewer decided a Nu. 1,627.50 receipt "exceeds the limit of 15000". | Photo run: 2 wrong decisions out of 22, both receipts. | Number comparisons moved into a `check_receipt_limit` tool; the reviewer may only use that reason when the tool says so. |
| The reviewer applied an invoice-only rule (missing PO) to a receipt. | Same run. | The validator skips PO checks for receipts; PO reason codes are marked invoice-only. |
| **The reviewer accepted an invoice whose purchase order the validator had just reported as closed.** | In 168 reviewer decisions after the fixes above, the reviewer alone accepted this same invoice twice. | **A code guardrail after the reviewer.** The second time, it overrode the acceptance (reviewer alone: 19/20 with 1 false accept; final: 20/20, 0). |
| Re-sending the conversation, image included, to the validator. | Same 19 documents: 8,783 vs 6,474 input tokens per document (+36%), same accuracy. One document with every model call printed: 9,921 vs 6,672. | The validator gets only its instructions and the extracted fields (ADK 2's own default for Workflow agents, which I had overridden by mistake). |
| The free tier's 15 requests per minute, and "model overloaded" (503) errors. | Several runs. | Retries with backoff (15 s doubling to 240 s); a failure is reported per document instead of stopping the run. |

## Data

60 synthetic documents (`python -m docintake.generate`, fixed seed): 36 invoices and 24
receipts across 14 scenarios (clean, already paid, PO amount mismatch, closed PO, another
vendor's PO, currency mismatch, totals that do not add up, unknown vendor, missing PO,
receipt over the limit). Layouts, labels, date and money formats vary, and there are
distractors such as bank account numbers. The correct answer for every document comes
from the business rules in `docintake/rules.py`. No real data is used.

## Run it

Commands use `python -m ...` because Windows Smart App Control can block the `.exe`
launchers pip creates in `.venv\Scripts` (`uvicorn.exe`, `adk.exe`).

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp .env.example .env                                 # add a Gemini API key
python -m docintake.generate                         # documents, ERP data, labels
python -m pytest                                     # no API key needed
python -m uvicorn mock_erp.app:app --port 8001       # in a second terminal
python -m google.adk.cli web agents                  # chat UI: upload a document
python -m docintake.eval --limit 10 --sleep 25       # measured run
python -m docintake.compare --before results/run-A.jsonl --after latest
python -m docintake.summary results/run-A.jsonl results/run-B.jsonl
```

The tests include an offline end-to-end run of the whole Workflow with scripted models,
a replay of the closed-PO incident that the guardrail must catch, and a check that the
guardrail's evidence equals the business rules on all 60 documents.

## Deploying on Google Cloud (not done yet)

Everything above ran on the Gemini API. Moving to Vertex AI needs no code change: set
`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, and
authenticate with Application Default Credentials. The agent and the mock ERP would run
on Cloud Run (`adk deploy cloud_run`), with keys in Secret Manager and traces in Cloud
Trace.

## Limitations

- Synthetic documents: varied, but generated by one program. Real invoices are messier;
  the next step is a set of real, redacted documents.
- 60 documents and one model. The model is a setting (`MODEL`); comparing models on cost
  and accuracy is the next experiment.
- Latency was measured on the free tier while the provider was under load, and varied
  between about 10 s and 20 s median across runs. Cost per document needs current prices
  (`PRICE_INPUT_PER_M`, `PRICE_OUTPUT_PER_M`).
- The guardrail's vendor match is exact (name or alias). A fuzzier real-world vendor
  master would need a reviewed matching rule.
