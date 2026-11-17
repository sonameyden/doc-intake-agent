"""Run the intake pipeline over the labelled documents and report how it did.

    python -m docintake.eval                  # every document
    python -m docintake.eval --limit 10       # first 10
    python -m docintake.eval --only photo     # by format: pdf, scan or photo
    python -m docintake.eval --dry-run        # no model: checks the scoring itself

Needs the mock ERP running (python -m uvicorn mock_erp.app:app --port 8001) and a model key in
.env. Writes every result to results/run-<time>.jsonl and a summary to
results/run-<time>.md. Set PRICE_INPUT_PER_M and PRICE_OUTPUT_PER_M (USD per million
tokens, from the current Gemini pricing page) to get cost per document.
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from docintake import DOCUMENTS, ERP_FILE, LABELS_FILE, ROOT
from docintake.rules import Fields, decide
from docintake.scoring import FIELDS, score

MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg"}
APP = "doc_intake_eval"


def _wanted_numbers(files: str | list[str]) -> set[int]:
    """Document numbers from "016,047", "16 47" or ["016", "047"]. PowerShell turns an
    unquoted 016,047 into the numbers 16 and 47, so match by number, not by text."""
    parts = files if isinstance(files, list) else [files]
    numbers = set()
    for part in parts:
        for item in part.replace(",", " ").split():
            if not item.isdigit():
                raise SystemExit(f"--files takes document numbers like 016,047; got {item!r}")
            numbers.add(int(item))
    return numbers


def load_labels(limit: int | None, only: str | None, files: str | list[str] | None = None) -> list[dict]:
    labels = [json.loads(line) for line in LABELS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if files:
        wanted = _wanted_numbers(files)
        labels = [label for label in labels if int(label["file"].split("_", 1)[0]) in wanted]
    if only:
        labels = [label for label in labels if only in (label["format"], label["scenario"], label["fields"]["document_type"])]
    return labels[:limit] if limit else labels


def _as_dict(value) -> dict | None:
    if value is None or isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


async def run_one(runner, label: dict) -> dict:
    from google.genai import types

    path = DOCUMENTS / label["file"]
    session = await runner.session_service.create_session(app_name=APP, user_id="eval")
    message = types.Content(role="user", parts=[
        types.Part.from_bytes(data=path.read_bytes(), mime_type=MIME[path.suffix]),
        types.Part(text="Process this document."),
    ])
    tokens_in = tokens_out = 0
    tool_calls: list[str] = []
    started = time.perf_counter()
    async for event in runner.run_async(user_id="eval", session_id=session.id, new_message=message):
        usage = getattr(event, "usage_metadata", None)
        if usage:
            tokens_in += usage.prompt_token_count or 0
            tokens_out += (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
        tool_calls += [call.name for call in event.get_function_calls()]
    latency = time.perf_counter() - started
    state = (await runner.session_service.get_session(app_name=APP, user_id="eval", session_id=session.id)).state
    return {
        "extracted": _as_dict(state.get("extracted")),
        "validation": state.get("validation"),
        "decision": _as_dict(state.get("decision")),
        "latency_s": round(latency, 2),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tool_calls": tool_calls,
    }


def dry_run_one(label: dict, erp: dict) -> dict:
    f = label["fields"]
    fields = Fields(f["document_type"], f["vendor_name"], f["invoice_number"], f["invoice_date"], f["currency"],
                    Decimal(f["subtotal"]), Decimal(f["tax"]), Decimal(f["total"]), f["po_number"])
    decision, reasons = decide(fields, erp)
    return {"extracted": f, "validation": None, "decision": {"decision": decision, "reasons": reasons, "summary": ""},
            "latency_s": 0.0, "tokens_in": 0, "tokens_out": 0, "tool_calls": []}


# Errors worth waiting out: rate limits (429) and an overloaded or briefly failing
# provider (500, 503, 504). Anything else is a real failure and is reported at once.
TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "503", "UNAVAILABLE", "504", "DEADLINE_EXCEEDED")


def is_transient(error: BaseException) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:  # ADK wraps the provider's error
        seen.add(id(error))
        if any(code in str(error) for code in TRANSIENT):
            return True
        error = error.__cause__ or error.__context__ or getattr(error, "error", None)
    return False


async def run_with_retry(runner, label: dict, retries: int = 5) -> dict:
    """Retry transient provider errors with exponential backoff (15, 30, 60 s, ...).
    Each attempt starts a fresh session, so a half-finished run never leaks state."""
    delay = 15.0
    for attempt in range(retries + 1):
        try:
            result = await run_one(runner, label)
            result["attempts"] = attempt + 1
            return result
        except Exception as error:  # noqa: BLE001 - provider errors arrive as several types
            if not is_transient(error) or attempt == retries:
                return {"error": f"{error.__class__.__name__}: {str(error)[:300]}", "attempts": attempt + 1}
            print(f"  provider busy or rate limited ({str(error)[:60]}...); retrying in {delay:.0f} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 240.0)
    raise AssertionError("unreachable")


def summarise(rows: list[dict]) -> str:
    n = len(rows)
    done = [r for r in rows if "error" not in r]
    pct = lambda k: 100 * k / n if n else 0.0  # noqa: E731
    price_in = float(os.getenv("PRICE_INPUT_PER_M", "0") or 0)
    price_out = float(os.getenv("PRICE_OUTPUT_PER_M", "0") or 0)
    lines = [f"# Eval run: {n} documents", ""]
    lines += ["| Metric | Value |", "| --- | --- |"]
    lines.append(f"| All fields correct | {sum(r['score']['all_fields'] for r in done)}/{n} ({pct(sum(r['score']['all_fields'] for r in done)):.1f}%) |")
    lines.append(f"| Decision correct | {sum(r['score']['decision_correct'] for r in done)}/{n} ({pct(sum(r['score']['decision_correct'] for r in done)):.1f}%) |")
    lines.append(f"| Reasons exactly right | {sum(r['score']['reasons_exact'] for r in done)}/{n} |")
    lines.append(f"| **False accepts** (needed a person, auto-accepted) | {sum(r['score']['false_accept'] for r in done)} |")
    lines.append(f"| False reviews (fine, sent to a person) | {sum(r['score']['false_review'] for r in done)} |")
    lines.append(f"| Errors (no result) | {n - len(done)} |")
    if done and any(r["latency_s"] for r in done):
        latencies = sorted(r["latency_s"] for r in done)
        lines.append(f"| Latency median / p90 | {statistics.median(latencies):.1f} s / {latencies[int(0.9 * (len(latencies) - 1))]:.1f} s |")
        avg_in = statistics.mean(r["tokens_in"] for r in done)
        avg_out = statistics.mean(r["tokens_out"] for r in done)
        lines.append(f"| Tokens per document (in / out) | {avg_in:,.0f} / {avg_out:,.0f} |")
        if price_in or price_out:
            lines.append(f"| Cost per document | ${(avg_in * price_in + avg_out * price_out) / 1e6:.5f} |")
        else:
            lines.append("| Cost per document | set PRICE_INPUT_PER_M / PRICE_OUTPUT_PER_M |")
    lines += ["", "## Field accuracy", "", "| Field | Correct |", "| --- | --- |"]
    for name in FIELDS:
        lines.append(f"| {name} | {sum(r['score']['fields'][name] for r in done)}/{n} |")
    lines += ["", "## By format", "", "| Format | Docs | All fields | Decision |", "| --- | --- | --- | --- |"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in done:
        groups[r["format"] + (" (hard)" if r["hard"] else "")].append(r)
    for key in sorted(groups):
        g = groups[key]
        lines.append(f"| {key} | {len(g)} | {sum(r['score']['all_fields'] for r in g)} | {sum(r['score']['decision_correct'] for r in g)} |")
    failures = [r for r in done if not r["score"]["decision_correct"] or not r["score"]["all_fields"]]
    if failures:
        lines += ["", "## Failures", "", "| File | Wrong fields | Expected | Got | Missed / extra reasons |", "| --- | --- | --- | --- | --- |"]
        for r in failures:
            wrong = ", ".join(k for k, ok in r["score"]["fields"].items() if not ok) or "-"
            got = (r.get("decision") or {}).get("decision")
            lines.append(f"| {r['file']} | {wrong} | {r['expected']} | {got} | {r['score']['missed_reasons']} / {r['score']['extra_reasons']} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", help="pdf, scan, photo, invoice, receipt or a scenario name")
    parser.add_argument("--files", nargs="+", help='document numbers, e.g. "016,047,048" or 16 47 48')
    parser.add_argument("--dry-run", action="store_true", help="no model; score the rules' own answers")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds between documents (free-tier rate limits)")
    parser.add_argument("--verbose", action="store_true", help="show ADK's own error tracebacks")
    args = parser.parse_args()
    if not args.verbose:
        # ADK logs a full traceback for every failed model call; this runner already
        # prints one line per document and retries transient errors.
        logging.getLogger("google_adk").setLevel(logging.CRITICAL)

    load_dotenv(find_dotenv(usecwd=True))
    labels = load_labels(args.limit, args.only, args.files)
    if not labels:
        raise SystemExit("No documents match --files/--only. Example: --files \"016,047,048\" or --only photo")
    erp = json.loads(ERP_FILE.read_text(encoding="utf-8"))

    runner = None
    if not args.dry_run:
        sys.path.insert(0, str(ROOT / "agents"))
        from google.adk.runners import InMemoryRunner

        from intake_agent.agent import root_agent

        runner = InMemoryRunner(agent=root_agent, app_name=APP)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-dry" if args.dry_run else "")
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    rows = []
    with (out_dir / f"run-{stamp}.jsonl").open("w", encoding="utf-8") as out:
        for i, label in enumerate(labels, 1):
            result = dry_run_one(label, erp) if args.dry_run else await run_with_retry(runner, label)
            row = {"file": label["file"], "format": label["format"], "hard": label["hard"],
                   "scenario": label["scenario"], "expected": label["decision"], "expected_reasons": label["reasons"], **result}
            if "error" not in row:
                row["score"] = score(label, row["extracted"], row["decision"])
                mark = "ok " if row["score"]["decision_correct"] and row["score"]["all_fields"] else "BAD"
                print(f"[{i}/{len(labels)}] {mark} {label['file']} -> {(row['decision'] or {}).get('decision')} ({row['latency_s']} s)")
            else:
                print(f"[{i}/{len(labels)}] ERR {label['file']}: {row['error']}")
            rows.append(row)
            out.write(json.dumps(row, default=str) + "\n")
            if args.sleep and not args.dry_run:
                await asyncio.sleep(args.sleep)

    report = summarise(rows)
    (out_dir / f"run-{stamp}.md").write_text(report, encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    asyncio.run(main())
