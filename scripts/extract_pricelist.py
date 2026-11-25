#!/usr/bin/env python3
"""Extract SKU, description, UOM, and raw price from vendor PDF, Excel, or CSV files.

Preserves leading zeros strictly as strings, assigns per-row confidence
(HIGH/MED/LOW), detects textless scanned PDF pages, and outputs structured JSON.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

HEADER_ALIASES = {
    "vendor_part_no": {
        "vendor_part_no", "vendor part no", "vendor part", "part_no", "part no",
        "part_number", "part number", "part#", "part #", "mpn", "mfg_part_no",
        "mfg part no", "item_no", "item no", "item_number", "item number",
        "sku", "catalog_no", "catalog no", "model", "model_no",
    },
    "description": {
        "description", "desc", "item_description", "item description",
        "product_description", "product description", "name", "item_name",
    },
    "uom": {
        "uom", "u/m", "u_m", "unit", "unit_of_measure", "unit of measure",
        "um", "pricing_unit", "pricing unit", "per",
    },
    "raw_price": {
        "raw_price", "price", "net_price", "net price", "net_cost", "net cost",
        "new_cost", "new cost", "new_price", "new price", "list_price",
        "list price", "cost", "unit_cost", "unit cost", "unit_price", "unit price",
        "2026_price", "2026 price", "wholesale_price", "wholesale price",
    },
}

PRICE_CLEAN_RE = re.compile(r"[^0-9.\-]")
LINE_FALLBACK_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,32})\s+(.+?)\s+"
    r"(EA|EACH|PC|PCS|C|M|BX|BOX|CS|CASE|PK|PKG|DZ|PR|FT|LB|RL|BG|/C|/M)\s+"
    r"\$?\s*([0-9,]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE,
)


def normalize_header(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def map_headers(raw_headers: list[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(raw_headers):
        norm = normalize_header(cell)
        for canonical, aliases in HEADER_ALIASES.items():
            if norm in aliases and canonical not in mapping:
                mapping[canonical] = idx
    return mapping


def parse_price(raw_val: Any) -> str | None:
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s:
        return None
    cleaned = PRICE_CLEAN_RE.sub("", s)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None
    if dec <= 0:
        return None
    return format(dec, "f")


def format_excel_cell_as_string(cell: Any) -> str:
    val = cell.value
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, int):
        fmt = str(cell.number_format or "").strip()
        if fmt and re.fullmatch(r"0+", fmt):
            return str(val).zfill(len(fmt))
        return str(val)
    if isinstance(val, float):
        if val.is_integer():
            fmt = str(cell.number_format or "").strip()
            if fmt and re.fullmatch(r"0+", fmt):
                return str(int(val)).zfill(len(fmt))
            return str(int(val))
        return str(val)
    return str(val).strip()


def score_row(part_no: str, desc: str, uom: str, price_str: str | None, via_fallback: bool = False) -> str:
    if not part_no or not price_str or not uom:
        return "LOW"
    if len(part_no) > 40 or "\n" in part_no or "  " in part_no:
        return "LOW"
    if via_fallback or not desc:
        return "MED"
    return "HIGH"


def build_row(
    part_no: str,
    description: str,
    uom: str,
    raw_price: Any,
    source_page: str,
    via_fallback: bool = False,
) -> dict[str, Any]:
    part_str = str(part_no).strip()
    desc_str = re.sub(r"\s+", " ", str(description or "")).strip()
    uom_str = str(uom or "").strip().upper()
    price_str = parse_price(raw_price)
    confidence = score_row(part_str, desc_str, uom_str, price_str, via_fallback=via_fallback)
    return {
        "vendor_part_no": part_str,
        "description": desc_str,
        "uom": uom_str,
        "raw_price": price_str,
        "raw_price_original": "" if raw_price is None else str(raw_price).strip(),
        "confidence": confidence,
        "source_page": source_page,
    }


def extract_csv(path: Path) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header_row = next(reader, None)
        if not header_row:
            return rows, [], ["CSV file is empty."]
        col_map = map_headers(header_row)
        if "vendor_part_no" not in col_map or "raw_price" not in col_map:
            warnings.append(f"Incomplete CSV header mapping: {col_map}")
        for line_no, raw in enumerate(reader, start=2):
            if not any(c.strip() for c in raw):
                continue
            part = raw[col_map["vendor_part_no"]] if "vendor_part_no" in col_map and col_map["vendor_part_no"] < len(raw) else ""
            desc = raw[col_map["description"]] if "description" in col_map and col_map["description"] < len(raw) else ""
            uom = raw[col_map["uom"]] if "uom" in col_map and col_map["uom"] < len(raw) else ""
            price = raw[col_map["raw_price"]] if "raw_price" in col_map and col_map["raw_price"] < len(raw) else ""
            rows.append(build_row(part, desc, uom, price, f"csv:row_{line_no}"))
    return rows, [], warnings


def extract_xlsx(path: Path, sheet_name: str | None) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    col_map: dict[str, int] = {}
    header_row_idx = 0
    for r_idx, row_cells in enumerate(ws.iter_rows(min_row=1, max_row=min(20, ws.max_row)), start=1):
        candidate = map_headers([c.value for c in row_cells])
        if "vendor_part_no" in candidate and "raw_price" in candidate:
            col_map = candidate
            header_row_idx = r_idx
            break

    if not col_map:
        warnings.append("Could not detect header row with part number and price columns in Excel sheet.")
        return rows, [], warnings

    for r_idx, row_cells in enumerate(ws.iter_rows(min_row=header_row_idx + 1, max_row=ws.max_row), start=header_row_idx + 1):
        vals = [c.value for c in row_cells]
        if not any(v is not None and str(v).strip() != "" for v in vals):
            continue
        p_idx = col_map["vendor_part_no"]
        part = format_excel_cell_as_string(row_cells[p_idx]) if p_idx < len(row_cells) else ""
        d_idx = col_map.get("description")
        desc = format_excel_cell_as_string(row_cells[d_idx]) if d_idx is not None and d_idx < len(row_cells) else ""
        u_idx = col_map.get("uom")
        uom = format_excel_cell_as_string(row_cells[u_idx]) if u_idx is not None and u_idx < len(row_cells) else ""
        pr_idx = col_map["raw_price"]
        price = row_cells[pr_idx].value if pr_idx < len(row_cells) else None
        rows.append(build_row(part, desc, uom, price, f"{ws.title}:row_{r_idx}"))
    return rows, [], warnings


def extract_pdf(path: Path) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    import pdfplumber

    rows: list[dict[str, Any]] = []
    scanned_pages: list[int] = []
    warnings: list[str] = []

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            tables = page.extract_tables() or []
            if len(text) < 10 and not tables:
                scanned_pages.append(page_num)
                continue

            page_rows_found = 0
            for table in tables:
                if not table:
                    continue
                col_map: dict[str, int] = {}
                start_idx = 0
                for idx, trow in enumerate(table[:5]):
                    cand = map_headers(trow)
                    if "vendor_part_no" in cand and "raw_price" in cand:
                        col_map = cand
                        start_idx = idx + 1
                        break
                if not col_map:
                    continue
                for trow in table[start_idx:]:
                    if not trow or not any(c and str(c).strip() for c in trow):
                        continue
                    part = trow[col_map["vendor_part_no"]] if col_map["vendor_part_no"] < len(trow) else ""
                    desc = trow[col_map["description"]] if "description" in col_map and col_map["description"] < len(trow) else ""
                    uom = trow[col_map["uom"]] if "uom" in col_map and col_map["uom"] < len(trow) else ""
                    price = trow[col_map["raw_price"]] if col_map["raw_price"] < len(trow) else ""
                    rows.append(build_row(str(part or ""), str(desc or ""), str(uom or ""), price, f"pdf:page_{page_num}"))
                    page_rows_found += 1

            if page_rows_found == 0 and text:
                for line in text.splitlines():
                    match = LINE_FALLBACK_RE.match(line)
                    if match:
                        part, desc, uom, price = match.groups()
                        rows.append(
                            build_row(part, desc, uom, price, f"pdf:page_{page_num}", via_fallback=True)
                        )
                        page_rows_found += 1
                if page_rows_found == 0:
                    warnings.append(f"Page {page_num} had text but no recognizable price table rows.")

    return rows, scanned_pages, warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="extract_pricelist.py",
        description="Extract SKUs, descriptions, UOMs, and raw prices from vendor PDF, Excel, or CSV files.",
    )
    parser.add_argument("--input", required=True, help="Path to incoming vendor price list (.pdf, .xlsx, or .csv)")
    parser.add_argument("--output", required=True, help="Path to write extracted JSON payload")
    parser.add_argument("--sheet", default=None, help="Optional Excel worksheet name (defaults to active sheet)")
    parser.add_argument(
        "--price-type",
        choices=["net", "list"],
        default="net",
        help="Whether incoming prices are Net Cost or List Price (default: net)",
    )
    parser.add_argument(
        "--multiplier",
        type=str,
        default=None,
        help="Optional trade-discount multiplier (for example, 0.55) when --price-type is list",
    )
    args = parser.parse_args()

    in_path = Path(args.input).resolve()
    if not in_path.is_file():
        print(json.dumps({"status": "error", "error": f"Input file not found: {in_path}"}))
        return 2

    ext = in_path.suffix.lower()
    if ext == ".csv":
        rows, scanned_pages, warnings = extract_csv(in_path)
    elif ext in {".xlsx", ".xlsm"}:
        rows, scanned_pages, warnings = extract_xlsx(in_path, args.sheet)
    elif ext == ".pdf":
        rows, scanned_pages, warnings = extract_pdf(in_path)
    else:
        print(json.dumps({"status": "error", "error": f"Unsupported file extension: {ext}"}))
        return 2

    needs_multiplier = args.price_type == "list" and args.multiplier is None
    conf_counts = {"HIGH": 0, "MED": 0, "LOW": 0}
    for r in rows:
        conf_counts[r["confidence"]] = conf_counts.get(r["confidence"], 0) + 1

    payload = {
        "source_file": str(in_path),
        "file_type": ext.lstrip("."),
        "price_type": args.price_type,
        "multiplier": args.multiplier,
        "needs_multiplier": needs_multiplier,
        "scanned_pages": scanned_pages,
        "warnings": warnings,
        "confidence_counts": conf_counts,
        "rows": rows,
    }

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    summary = {
        "status": "needs_multiplier" if needs_multiplier else "ok",
        "output": str(out_path),
        "rows_extracted": len(rows),
        "confidence_counts": conf_counts,
        "scanned_pages": scanned_pages,
        "warnings": warnings,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
