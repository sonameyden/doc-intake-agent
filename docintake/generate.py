"""Generate the synthetic test set: documents, the mock ERP's data and the labels.

    python -m docintake.generate

Everything is made up (fixed seed, so the output is the same each run). Each document
is built from a scenario (clean, amount mismatch, already paid, ...), rendered as a PDF,
a clean scan (PNG) or a phone photo (JPG: tilted, blurred, noisy), and labelled with the
printed fields and the decision ``docintake.rules`` reaches for it.
"""

import json
import random
import shutil
from dataclasses import asdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from docintake import DOCUMENTS, ERP_FILE, LABELS_FILE
from docintake.rules import RECEIPT_LIMITS, Fields, decide

SEED = 7
CENT = Decimal("0.01")

VENDORS = [
    {"id": "V001", "name": "Druk Office Supplies", "aliases": ["Druk Office Supplies Pvt. Ltd."], "currency": "BTN"},
    {"id": "V002", "name": "Thimphu Print House", "aliases": [], "currency": "BTN"},
    {"id": "V003", "name": "Paro Valley Catering", "aliases": ["Paro Valley Catering Services"], "currency": "BTN"},
    {"id": "V004", "name": "Himalayan Tech Traders", "aliases": [], "currency": "INR"},
    {"id": "V005", "name": "Bumthang Hardware", "aliases": [], "currency": "BTN"},
    {"id": "V006", "name": "Kolkata Paper Mills", "aliases": ["Kolkata Paper Mills Ltd"], "currency": "INR"},
    {"id": "V007", "name": "Phuentsholing Logistics", "aliases": [], "currency": "INR"},
    {"id": "V008", "name": "Summit Cloud Services", "aliases": ["Summit Cloud Services Inc."], "currency": "USD"},
]
UNKNOWN_VENDORS = ["Yangchen General Store", "Norbu Motors", "Tashi Electronics", "Gakyil Stationers", "Dorji Fuel Station"]
ITEMS = {
    "BTN": [("A4 paper ream", 380), ("Printer toner", 4200), ("Lunch buffet (per head)", 650), ("Ethernet cable 10m", 900),
            ("Office chair", 7800), ("Banner printing", 2500), ("Hardware fittings", 1200), ("Tea and snacks", 180)],
    "INR": [("Laptop bag", 1800), ("Freight charges", 9500), ("Paper roll", 1250), ("Router", 4300), ("Packaging", 700)],
    "USD": [("Cloud hosting (monthly)", 85), ("Support hours", 40), ("Domain renewal", 18), ("Backup storage", 25)],
}
TAX_RATES = [Decimal("0"), Decimal("0.05"), Decimal("0.07")]

# (scenario, document_type, count)
SCENARIOS = [
    ("clean", "invoice", 12),
    ("po_amount_mismatch", "invoice", 4),
    ("unknown_vendor", "invoice", 3),
    ("already_paid", "invoice", 4),
    ("totals_mismatch", "invoice", 3),
    ("missing_po", "invoice", 3),
    ("po_closed", "invoice", 2),
    ("currency_mismatch", "invoice", 2),
    ("wrong_vendor_po", "invoice", 3),
    ("clean", "receipt", 12),
    ("over_receipt_limit", "receipt", 4),
    ("unknown_vendor", "receipt", 3),
    ("already_paid", "receipt", 2),
    ("totals_mismatch", "receipt", 3),
]

MONEY_STYLES = {"BTN": ["Nu. {}", "BTN {}"], "INR": ["Rs. {}", "INR {}"], "USD": ["${}", "USD {}"]}
DATE_LABELS = ["Date", "Invoice Date", "Dated", "Date of Issue"]
NUMBER_LABELS = ["Invoice No", "Invoice #", "Bill No.", "Invoice Number"]
PO_LABELS = ["PO Number", "Purchase Order", "Your Ref (PO)", "P.O. No."]
TOTAL_LABELS = ["TOTAL", "Amount Due", "Grand Total", "Total Payable"]


def money(value: Decimal, currency: str, rng: random.Random) -> str:
    return rng.choice(MONEY_STYLES[currency]).format(f"{value:,.2f}")


def fmt_date(d: date, rng: random.Random) -> str:
    # dd/mm/yyyy only when the day cannot be read as a month, so every date has one answer.
    formats = ["%d %b %Y", "%Y-%m-%d", "%B %d, %Y"] + (["%d/%m/%Y"] if d.day > 12 else [])
    return d.strftime(rng.choice(formats))


def build(index: int, scenario: str, doc_type: str, rng: random.Random, erp: dict) -> tuple[dict, Fields]:
    known = rng.choice(VENDORS)
    currency = known["currency"]
    vendor_name = rng.choice(UNKNOWN_VENDORS) if scenario == "unknown_vendor" else rng.choice([known["name"], *known["aliases"]])
    if doc_type == "receipt" and scenario != "over_receipt_limit" and currency != "USD":
        lines = [(n, Decimal(p)) for n, p in rng.sample(ITEMS[currency], k=rng.randint(1, 3))]
        lines = [(n, p, rng.randint(1, 3)) for n, p in lines]
        # Keep clean receipts under the limit.
        while sum(p * q for _, p, q in lines) > Decimal("9000") and len(lines) > 1:
            lines.pop()
        if sum(p * q for _, p, q in lines) > Decimal("9000"):
            lines = [(lines[0][0], Decimal("850"), 2)]
    else:
        lines = [(n, Decimal(p), rng.randint(1, 4 if doc_type == "invoice" else 3))
                 for n, p in rng.sample(ITEMS[currency], k=rng.randint(1, 4))]
    if doc_type == "receipt" and currency == "USD" and scenario != "over_receipt_limit":
        lines = [(lines[0][0], lines[0][1], 1)]
    if scenario == "over_receipt_limit":
        name, _ = rng.choice(ITEMS[currency])
        lines = [(name, (RECEIPT_LIMITS[currency] * Decimal(rng.choice(["1.3", "1.8", "2.5"]))).quantize(CENT), 1)]

    subtotal = sum((p * q for _, p, q in lines), Decimal("0")).quantize(CENT)
    rate = rng.choice(TAX_RATES)
    tax = (subtotal * rate).quantize(CENT, ROUND_HALF_UP)
    total = subtotal + tax
    if scenario == "totals_mismatch":
        total += rng.choice([Decimal("10"), Decimal("100"), Decimal("-50")])

    issued = date(2026, 9, 1) + timedelta(days=rng.randint(0, 75))
    prefix = "".join(w[0] for w in known["name"].split()[:3]).upper()
    number = f"{prefix}-{2026}-{index:04d}" if doc_type == "invoice" else f"R{rng.randint(10000, 99999)}"

    po_number = None
    if doc_type == "invoice" and scenario != "missing_po":
        po_number = f"PO-{rng.randint(4000, 4999)}"
        while any(p["po_number"] == po_number for p in erp["purchase_orders"]):
            po_number = f"PO-{rng.randint(4000, 4999)}"
        po = {"po_number": po_number, "vendor_id": known["id"], "currency": currency, "amount": str(total), "status": "open"}
        if scenario == "po_amount_mismatch":
            po["amount"] = str((total * Decimal(rng.choice(["0.80", "0.88", "1.12", "1.25"]))).quantize(CENT))
        elif scenario == "po_closed":
            po["status"] = "closed"
        elif scenario == "currency_mismatch":
            po["currency"] = rng.choice([c for c in ("BTN", "INR", "USD") if c != currency])
        elif scenario == "wrong_vendor_po":
            po["vendor_id"] = rng.choice([v["id"] for v in VENDORS if v["id"] != known["id"]])
        if scenario != "unknown_vendor":
            erp["purchase_orders"].append(po)
    if scenario == "already_paid":
        erp["payments"].append({"invoice_number": number, "vendor_id": known["id"], "amount": str(total),
                                "currency": currency, "paid_on": (issued + timedelta(days=10)).isoformat()})

    fields = Fields(doc_type, vendor_name, number, issued.isoformat(), currency, subtotal, tax, total, po_number)
    doc = {"fields": fields, "lines": lines, "rate": rate, "issued": issued}
    return doc, fields


def layout(doc: dict, rng: random.Random) -> list[dict]:
    """Rows of cells: [{"cells": [(text, x 0..1, "l"|"r")], "size": pt, "bold": bool}]."""
    f: Fields = doc["fields"]
    cur = f.currency

    def row(*cells, size=10, bold=False):
        return {"cells": list(cells), "size": size, "bold": bold}

    rows = [row((f.vendor_name, 0.0, "l"), size=18, bold=True),
            row((rng.choice(["Norzin Lam, Thimphu", "Paro Town", "Phuentsholing, Chukha", "Salt Lake, Kolkata"]), 0.0, "l"), size=9)]
    if f.document_type == "invoice":
        rows += [row((rng.choice(["TAX INVOICE", "INVOICE", "BILL"]), 1.0, "r"), size=14, bold=True),
                 row((f"{rng.choice(NUMBER_LABELS)}: {f.invoice_number}", 0.0, "l"), (f"{rng.choice(DATE_LABELS)}: {fmt_date(doc['issued'], rng)}", 1.0, "r"))]
        if f.po_number:
            rows.append(row((f"{rng.choice(PO_LABELS)}: {f.po_number}", 0.0, "l")))
        rows.append(row(("Bill To: Royal Tour Company, Thimphu", 0.0, "l"), size=9))
    else:
        rows += [row(("RECEIPT", 0.5, "l"), size=13, bold=True),
                 row((f"Receipt No: {f.invoice_number}", 0.0, "l")),
                 row((f"Date: {fmt_date(doc['issued'], rng)}", 0.0, "l"))]
    rows.append(row(("", 0, "l")))
    rows.append(row(("Item", 0.0, "l"), ("Qty", 0.55, "r"), ("Rate", 0.75, "r"), ("Amount", 1.0, "r"), bold=True))
    for name, price, qty in doc["lines"]:
        rows.append(row((name, 0.0, "l"), (str(qty), 0.55, "r"), (f"{price:,.2f}", 0.75, "r"), (f"{price * qty:,.2f}", 1.0, "r")))
    rows.append(row(("", 0, "l")))
    rows.append(row(("Subtotal", 0.6, "l"), (money(f.subtotal, cur, rng), 1.0, "r")))
    tax_label = "Tax" if doc["rate"] == 0 else f"GST {int(doc['rate'] * 100)}%"
    rows.append(row((tax_label, 0.6, "l"), (money(f.tax, cur, rng), 1.0, "r")))
    rows.append(row((rng.choice(TOTAL_LABELS) if f.document_type == "invoice" else "TOTAL", 0.6, "l"),
                    (money(f.total, cur, rng), 1.0, "r"), size=12, bold=True))
    rows.append(row(("", 0, "l")))
    footer = (f"Bank of Bhutan A/C {rng.randint(200000000, 299999999)}" if f.document_type == "invoice"
              else rng.choice(["Paid: Cash", "Paid: Card ****4417", "Paid: mBoB"]) + "  Thank you!")
    rows.append(row((footer, 0.0, "l"), size=8))
    return rows


def render_pdf(rows: list[dict], path: Path) -> None:
    width, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)
    left, right, y = 50, width - 50, height - 60
    for r in rows:
        font = "Helvetica-Bold" if r["bold"] else "Helvetica"
        pdf.setFont(font, r["size"])
        for text, x, align in r["cells"]:
            px = left + x * (right - left)
            (pdf.drawRightString if align == "r" else pdf.drawString)(px, y, text)
        y -= r["size"] + 8
    pdf.save()


def _font(size: int, bold: bool) -> ImageFont.ImageFont:
    for name in (["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_image(rows: list[dict], width: int) -> Image.Image:
    scale = 2
    height = sum(r["size"] + 8 for r in rows) * scale + 120
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    left, right, y = 40, width - 40, 50
    for r in rows:
        font = _font(r["size"] * scale, r["bold"])
        for text, x, align in r["cells"]:
            px = left + x * (right - left)
            if align == "r":
                px -= draw.textlength(text, font=font)
            draw.text((px, y), text, fill="black", font=font)
        y += (r["size"] + 8) * scale
    return img


def photograph(img: Image.Image, rng: random.Random, hard: bool) -> Image.Image:
    """Make a clean render look like a phone photo of paper on a desk."""
    angle = rng.uniform(4, 9) * rng.choice([-1, 1]) if hard else rng.uniform(1, 3) * rng.choice([-1, 1])
    img = img.rotate(angle, expand=True, fillcolor=(rng.randint(90, 140),) * 3, resample=Image.Resampling.BICUBIC)
    noise = Image.effect_noise(img.size, 40 if hard else 18).convert("RGB")
    img = Image.blend(img, noise, 0.18 if hard else 0.08)
    img = img.filter(ImageFilter.GaussianBlur(1.6 if hard else 0.7))
    img = ImageEnhance.Contrast(img).enhance(0.65 if hard else 0.9)
    return img


def main() -> None:
    rng = random.Random(SEED)
    erp = {"vendors": VENDORS, "purchase_orders": [], "payments": []}
    if DOCUMENTS.exists():
        shutil.rmtree(DOCUMENTS)
    DOCUMENTS.mkdir(parents=True)
    labels = []
    index = 0
    for scenario, doc_type, count in SCENARIOS:
        for _ in range(count):
            index += 1
            doc, fields = build(index, scenario, doc_type, rng, erp)
            rows = layout(doc, rng)
            if doc_type == "invoice":
                kind = rng.choices(["pdf", "scan", "photo"], weights=[6, 2, 2])[0]
            else:
                kind = rng.choices(["scan", "photo"], weights=[1, 2])[0]
            hard = kind == "photo" and rng.random() < 0.4
            stem = f"{index:03d}_{doc_type}_{scenario}"
            if kind == "pdf":
                name = f"{stem}.pdf"
                render_pdf(rows, DOCUMENTS / name)
            else:
                img = render_image(rows, 1400 if doc_type == "invoice" else 760)
                if kind == "photo":
                    name = f"{stem}.jpg"
                    photograph(img, rng, hard).save(DOCUMENTS / name, quality=70)
                else:
                    name = f"{stem}.png"
                    img.save(DOCUMENTS / name)
            labels.append({
                "file": name, "scenario": scenario, "format": kind, "hard": hard,
                "fields": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(fields).items()},
                "_fields": fields,
            })

    # Unrelated history, so lookups have something to search past.
    for i in range(12):
        vendor = rng.choice(VENDORS)
        erp["purchase_orders"].append({"po_number": f"PO-3{i:03d}", "vendor_id": vendor["id"], "currency": vendor["currency"],
                                       "amount": f"{rng.randint(1000, 90000)}.00", "status": rng.choice(["open", "closed"])})
    # Label against the finished ERP, exactly as the agent will see it.
    for label in labels:
        label["decision"], label["reasons"] = decide(label.pop("_fields"), erp)
    ERP_FILE.write_text(json.dumps(erp, indent=2), encoding="utf-8")
    LABELS_FILE.write_text("".join(json.dumps(label) + "\n" for label in labels), encoding="utf-8")

    accepted = sum(label["decision"] == "auto_accept" for label in labels)
    print(f"{len(labels)} documents in {DOCUMENTS} ({accepted} auto_accept, {len(labels) - accepted} needs_review)")


if __name__ == "__main__":
    main()
