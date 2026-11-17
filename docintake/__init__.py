from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCUMENTS = DATA / "documents"
ERP_FILE = DATA / "erp.json"
LABELS_FILE = DATA / "labels.jsonl"
