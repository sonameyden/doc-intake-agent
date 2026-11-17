"""Compare two sets of eval runs on the documents they have in common.

    python -m docintake.compare --before results/run-A.jsonl results/run-B.jsonl \
                                --after  results/run-C.jsonl

"latest" stands for the newest finished run, e.g. ``--after latest``.

Prints a Markdown table: accuracy, false accepts, latency and tokens before and after,
counted only over documents present (without errors) on both sides, so a smaller
"after" run is still a fair comparison.
"""

import argparse
import json
import statistics
from pathlib import Path

from docintake import ROOT


def resolve(path: str) -> Path:
    """A results file; "latest" means the newest finished (non-dry) run."""
    if path != "latest":
        found = Path(path)
        if not found.exists() and (ROOT / path).exists():
            found = ROOT / path
        if not found.exists():
            raise SystemExit(f"No such results file: {path}. Use a name from the results folder, or 'latest'.")
        return found
    runs = sorted(p for p in (ROOT / "results").glob("run-*.jsonl")
                  if not p.stem.endswith("-dry") and p.with_suffix(".md").exists())
    if not runs:
        raise SystemExit("No finished runs in results/ yet.")
    return runs[-1]


def load(paths: list[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for path in map(resolve, paths):
        print(f"reading {path.name}")
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "error" not in row and "score" in row:
                rows[row["file"]] = row  # a later file wins for the same document
    return rows


def describe(rows: list[dict]) -> dict:
    latencies = [r["latency_s"] for r in rows]
    return {
        "Decisions correct": f"{sum(r['score']['decision_correct'] for r in rows)}/{len(rows)}",
        "All fields correct": f"{sum(r['score']['all_fields'] for r in rows)}/{len(rows)}",
        "False accepts": str(sum(r["score"]["false_accept"] for r in rows)),
        "Latency median": f"{statistics.median(latencies):.1f} s",
        "Latency p90": f"{sorted(latencies)[int(0.9 * (len(latencies) - 1))]:.1f} s",
        "Input tokens / doc": f"{statistics.mean(r['tokens_in'] for r in rows):,.0f}",
        "Output tokens / doc": f"{statistics.mean(r['tokens_out'] for r in rows):,.0f}",
        "_latency": statistics.median(latencies),
        "_tokens_in": statistics.mean(r["tokens_in"] for r in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", nargs="+", required=True)
    parser.add_argument("--after", nargs="+", required=True)
    args = parser.parse_args()

    before, after = load(args.before), load(args.after)
    common = sorted(set(before) & set(after))
    if not common:
        raise SystemExit("No documents in common (without errors) between the two sides.")
    b, a = describe([before[f] for f in common]), describe([after[f] for f in common])
    print(f"Compared on {len(common)} documents present in both.\n")
    print("| Metric | Before | After |\n| --- | --- | --- |")
    for key in (k for k in b if not k.startswith("_")):
        print(f"| {key} | {b[key]} | {a[key]} |")
    print(f"\nLatency median: {100 * (a['_latency'] - b['_latency']) / b['_latency']:+.0f}%. "
          f"Input tokens: {100 * (a['_tokens_in'] - b['_tokens_in']) / b['_tokens_in']:+.0f}%.")
    changed = [f for f in common if before[f]["score"]["decision_correct"] != after[f]["score"]["decision_correct"]]
    if changed:
        print("\nDecision changed on: " + ", ".join(changed))


if __name__ == "__main__":
    main()
