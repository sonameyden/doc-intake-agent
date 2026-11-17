"""Merge eval runs into one report, e.g. a full set run in parts.

    python -m docintake.summary results/run-A.jsonl results/run-B.jsonl results/run-C.jsonl

For a document in several files, the later file wins (so a rerun replaces an error).
Prints the same report as ``docintake.eval`` and refuses to mix settings.
"""

import argparse
import json
from pathlib import Path

from docintake.eval import summarise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    rows: dict[str, dict] = {}
    configs = set()
    for path in args.files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            configs.add(json.dumps(row.get("config"), sort_keys=True))
            rows[row["file"]] = row
    if len(configs) > 1:
        raise SystemExit("These runs used different settings: " + ", ".join(sorted(configs)))
    merged = [rows[name] for name in sorted(rows)]
    print(f"Merged {len(args.files)} runs: {len(merged)} documents.\n")
    print(summarise(merged))


if __name__ == "__main__":
    main()
