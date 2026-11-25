#!/usr/bin/env python3
"""Deterministically diff extracted vendor price rows against an ERP item master CSV.

Uses exact Decimal math, normalizes part numbers while preserving leading zeros as
quoted strings, detects UOM traps (including C/100, M/1000, and >40% swings),
isolates exceptions, and exports schema-validated ERP bulk-upload CSV plus an
executive price-change impact report.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

PART_NORM_RE = re.compile(r"[^A-Za-z0-9]")

EXCEPTION_COLUMNS = [
    "vendor_part_no",
    "erp_sku",
    "exception_type",
    "old_cost",
    "new_cost",
    "pct_change",
    "old_uom",
    "new_uom",
    "confidence",
    "source_page",
]


def normalize_part_key(value: str) -> str:
    return PART_NORM_RE.sub("", str(value or "").strip()).upper()


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def fmt_dec(value: Decimal | None, places: int = 4) -> str:
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    return str(value.quantize(quant, rounding=ROUND_HALF_UP))


def load_uom_table(path: Path) -> tuple[dict[str, str], set[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    alias_map: dict[str, str] = {}
    high_risk: set[str] = {str(t).upper() for t in data.get("high_risk_tokens", [])}
    for canonical, spec in data.get("canonical_uoms", {}).items():
        canon_up = str(canonical).upper()
        alias_map[canon_up] = canon_up
        if spec.get("high_risk_multiplier_trap"):
            high_risk.add(canon_up)
        for alias in spec.get("aliases", []):
            alias_map[str(alias).strip().upper()] = canon_up
    return alias_map, high_risk


def normalize_uom(raw: str, alias_map: dict[str, str]) -> str:
    cleaned = re.sub(r"\s+", " ", str(raw or "").strip().upper())
    return alias_map.get(cleaned, cleaned)


def load_erp_master(path: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError("ERP master CSV has no header row.")
        for row in reader:
            cleaned = {str(k).strip().lower(): (v.strip() if v is not None else "") for k, v in row.items()}
            if not cleaned.get("sku") and not cleaned.get("vendor_part_no"):
                continue
            items.append(cleaned)
    return items


def write_erp_upload_csv(
    path: Path,
    clean_rows: list[dict[str, Any]],
    profile: dict[str, Any],
) -> None:
    columns = profile["columns"]
    delimiter = profile.get("delimiter", ",")
    dec_places = int(profile.get("decimal_places", 4))
    quote_strings = bool(profile.get("quote_string_columns", True))

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(
            fh,
            delimiter=delimiter,
            quoting=csv.QUOTE_ALL if quote_strings else csv.QUOTE_MINIMAL,
        )
        writer.writerow([col["target"] for col in columns])
        for row in clean_rows:
            out_vals: list[str] = []
            for col in columns:
                src = col["source"]
                ctype = col.get("type", "string")
                raw_val = row.get(src, col.get("default", ""))
                if ctype == "decimal":
                    out_vals.append(fmt_dec(raw_val, dec_places) if isinstance(raw_val, Decimal) else str(raw_val or ""))
                else:
                    out_vals.append("" if raw_val is None else str(raw_val))
            writer.writerow(out_vals)


def write_exceptions_csv(path: Path, exceptions: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for exc in exceptions:
            writer.writerow({k: exc.get(k, "") for k in EXCEPTION_COLUMNS})


def write_impact_report(
    path: Path,
    source_file: str,
    erp_profile_name: str,
    matched_count: int,
    clean_count: int,
    avg_pct_change: Decimal | None,
    exceptions: list[dict[str, Any]],
    margin_erosion_rows: list[dict[str, Any]],
    min_margin_pct: Decimal,
) -> None:
    exc_counts: dict[str, int] = {}
    for exc in exceptions:
        etype = exc["exception_type"]
        exc_counts[etype] = exc_counts.get(etype, 0) + 1

    lines: list[str] = [
        "# Price Change Impact Report",
        "",
        f"- **Source Price List**: `{source_file}`",
        f"- **Target ERP Profile**: `{erp_profile_name}`",
        f"- **Total SKUs Matched**: {matched_count}",
        f"- **ERP-Ready Clean Matches (`erp_ready_upload.csv`)**: {clean_count}",
        f"- **Average Cost Change (Matched Non-UOM-Error SKUs)**: "
        + (f"{fmt_dec(avg_pct_change, 2)}%" if avg_pct_change is not None else "N/A"),
        f"- **Total Exceptions Requiring Purchasing Sign-Off**: {len(exceptions)}",
        "",
        "## Exception Breakdown",
        "",
        "| Exception Type | Count |",
        "| --- | --- |",
    ]
    if exc_counts:
        for etype, cnt in sorted(exc_counts.items()):
            lines.append(f"| {etype} | {cnt} |")
    else:
        lines.append("| None | 0 |")

    lines.extend([
        "",
        f"## Top 20 Margin-Eroding SKUs (Margin Floor: {fmt_dec(min_margin_pct, 1)}%, Excluding UOM_MISMATCH)",
        "",
        "| ERP SKU | Vendor Part No | Old Cost | New Cost | Cost Change % | Sell Price | New Margin % | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    top20 = sorted(margin_erosion_rows, key=lambda r: r["new_margin_pct"])[:20]
    if top20:
        for r in top20:
            lines.append(
                f"| `{r['erp_sku']}` | `{r['vendor_part_no']}` | "
                f"${fmt_dec(r['old_cost'], 4)} | ${fmt_dec(r['new_cost'], 4)} | "
                f"{fmt_dec(r['pct_change'], 2)}% | ${fmt_dec(r['sell_price'], 4)} | "
                f"{fmt_dec(r['new_margin_pct'], 2)}% | {r['status']} |"
            )
    else:
        lines.append("| None | - | - | - | - | - | - | No sell_price margin data |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_uom = script_dir.parent / "references" / "uom_normalization_table.yaml"
    default_schema = script_dir.parent / "assets" / "erp_upload_schema_sample.yaml"

    parser = argparse.ArgumentParser(
        prog="diff_erp_costs.py",
        description="Deterministically diff extracted vendor prices against an ERP item master CSV.",
    )
    parser.add_argument("--extracted", required=True, help="Path to JSON file produced by extract_pricelist.py")
    parser.add_argument("--erp-master", required=True, help="Path to current ERP item master CSV")
    parser.add_argument("--output-dir", required=True, help="Directory for output CSVs and Markdown report")
    parser.add_argument("--uom-table", default=str(default_uom), help="Path to uom_normalization_table.yaml")
    parser.add_argument("--schema", default=str(default_schema), help="Path to ERP upload schema YAML")
    parser.add_argument("--erp-profile", default=None, help="Profile name in schema YAML (default: default_profile)")
    parser.add_argument("--multiplier", default=None, help="Trade-discount multiplier for List Price files")
    parser.add_argument("--effective-date", default=dt.date.today().isoformat(), help="Effective date (YYYY-MM-DD)")
    parser.add_argument("--max-increase-pct", default="10.0", help="Cost spike threshold percent (default: 10.0)")
    parser.add_argument("--max-decrease-pct", default="20.0", help="Cost drop UOM-suspect threshold percent (default: 20.0)")
    parser.add_argument("--uom-swing-pct", default="40.0", help="Sudden swing UOM_MISMATCH threshold percent (default: 40.0)")
    parser.add_argument("--min-margin-pct", default="25.0", help="Minimum gross margin percent floor (default: 25.0)")
    parser.add_argument("--no-flag-discontinued", action="store_true", help="Do not flag unmatched ERP SKUs as DISCONTINUED_SKU")
    args = parser.parse_args()

    if yaml is None:
        print(
            json.dumps(
                {
                    "status": "MISSING_DEPENDENCY",
                    "error": "PyYAML is required. Install with: pip install PyYAML",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    extracted_path = Path(args.extracted).resolve()
    erp_path = Path(args.erp_master).resolve()
    uom_path = Path(args.uom_table).resolve()
    schema_path = Path(args.schema).resolve()
    out_dir = Path(args.output_dir).resolve()

    extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
    multiplier_raw = args.multiplier if args.multiplier is not None else extracted.get("multiplier")
    if extracted.get("needs_multiplier") and multiplier_raw is None:
        print(json.dumps({
            "status": "needs_multiplier",
            "message": "Vendor file uses List Price without a trade-discount multiplier. Ask the user for the multiplier before running diff.",
        }, indent=2))
        return 3

    multiplier = to_decimal(multiplier_raw) if multiplier_raw is not None else Decimal("1")
    if multiplier is None or multiplier <= 0:
        print(json.dumps({"status": "error", "error": f"Invalid multiplier: {multiplier_raw}"}))
        return 2

    max_inc = Decimal(args.max_increase_pct)
    max_dec = Decimal(args.max_decrease_pct)
    uom_swing = Decimal(args.uom_swing_pct)
    min_margin = Decimal(args.min_margin_pct)

    alias_map, high_risk_uoms = load_uom_table(uom_path)
    schema_data = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    profile_name = args.erp_profile or schema_data.get("default_profile", "epicor_p21")
    if profile_name not in schema_data.get("profiles", {}):
        print(json.dumps({"status": "error", "error": f"ERP profile not found: {profile_name}"}))
        return 2
    profile = schema_data["profiles"][profile_name]

    erp_items = load_erp_master(erp_path)
    erp_by_vpart: dict[str, dict[str, str]] = {}
    erp_by_sku: dict[str, dict[str, str]] = {}
    for item in erp_items:
        vp_key = normalize_part_key(item.get("vendor_part_no", ""))
        sku_key = normalize_part_key(item.get("sku", ""))
        if vp_key:
            erp_by_vpart[vp_key] = item
        if sku_key:
            erp_by_sku[sku_key] = item

    clean_rows: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    margin_erosion_rows: list[dict[str, Any]] = []
    matched_erp_skus: set[str] = set()
    pct_changes_for_avg: list[Decimal] = []
    matched_count = 0

    for page_num in extracted.get("scanned_pages", []):
        exceptions.append({
            "vendor_part_no": "",
            "erp_sku": "",
            "exception_type": "SCANNED_PDF_REVIEW",
            "old_cost": "",
            "new_cost": "",
            "pct_change": "",
            "old_uom": "",
            "new_uom": "",
            "confidence": "LOW",
            "source_page": f"pdf:page_{page_num}",
        })

    for row in extracted.get("rows", []):
        vpart = str(row.get("vendor_part_no", "")).strip()
        vkey = normalize_part_key(vpart)
        conf = str(row.get("confidence", "LOW")).upper()
        source_page = str(row.get("source_page", ""))
        raw_price_dec = to_decimal(row.get("raw_price"))
        new_cost = (raw_price_dec * multiplier) if raw_price_dec is not None else None
        new_uom_raw = str(row.get("uom", "")).strip().upper()
        new_uom_norm = normalize_uom(new_uom_raw, alias_map)

        erp_match = erp_by_vpart.get(vkey) or erp_by_sku.get(vkey)
        if not erp_match:
            exceptions.append({
                "vendor_part_no": vpart,
                "erp_sku": "",
                "exception_type": "NEW_SKU",
                "old_cost": "",
                "new_cost": fmt_dec(new_cost, 4),
                "pct_change": "",
                "old_uom": "",
                "new_uom": new_uom_norm,
                "confidence": conf,
                "source_page": source_page,
            })
            continue

        matched_count += 1
        erp_sku = erp_match.get("sku", "")
        matched_erp_skus.add(erp_sku)
        old_cost = to_decimal(erp_match.get("current_cost"))
        sell_price = to_decimal(erp_match.get("sell_price"))
        old_uom_norm = normalize_uom(erp_match.get("uom", ""), alias_map)

        abs_delta: Decimal | None = None
        pct_change: Decimal | None = None
        if old_cost is not None and old_cost > 0 and new_cost is not None:
            abs_delta = new_cost - old_cost
            pct_change = (abs_delta / old_cost) * Decimal("100")

        new_margin_pct: Decimal | None = None
        if sell_price is not None and sell_price > 0 and new_cost is not None:
            new_margin_pct = ((sell_price - new_cost) / sell_price) * Decimal("100")

        uom_trap = (
            (old_uom_norm != new_uom_norm)
            or (new_uom_norm in high_risk_uoms)
            or (pct_change is not None and abs(pct_change) > uom_swing)
        )

        row_flags: list[str] = []
        if uom_trap:
            row_flags.append("UOM_MISMATCH")
        else:
            if new_cost is None or old_cost is None or conf == "LOW":
                row_flags.append("LOW_CONFIDENCE")
            elif conf == "MED":
                row_flags.append("MED_CONFIDENCE")
            if new_margin_pct is not None and new_margin_pct < min_margin:
                row_flags.append("MARGIN_BREACH")
            if pct_change is not None and pct_change > max_inc:
                row_flags.append("COST_SPIKE")
            if pct_change is not None and pct_change < -max_dec:
                row_flags.append("COST_DROP_UOM_SUSPECT")

        if not uom_trap and pct_change is not None:
            pct_changes_for_avg.append(pct_change)

        if not uom_trap and new_margin_pct is not None and old_cost is not None and new_cost is not None and pct_change is not None and sell_price is not None:
            margin_erosion_rows.append({
                "erp_sku": erp_sku,
                "vendor_part_no": vpart,
                "old_cost": old_cost,
                "new_cost": new_cost,
                "pct_change": pct_change,
                "sell_price": sell_price,
                "new_margin_pct": new_margin_pct,
                "status": "|".join(row_flags) if row_flags else "CLEAN",
            })

        if row_flags:
            exceptions.append({
                "vendor_part_no": vpart,
                "erp_sku": erp_sku,
                "exception_type": "|".join(row_flags),
                "old_cost": fmt_dec(old_cost, 4),
                "new_cost": fmt_dec(new_cost, 4),
                "pct_change": fmt_dec(pct_change, 2),
                "old_uom": old_uom_norm,
                "new_uom": new_uom_norm,
                "confidence": conf,
                "source_page": source_page,
            })
        else:
            clean_rows.append({
                "erp_sku": erp_sku,
                "vendor_part_no": vpart,
                "old_uom": old_uom_norm,
                "new_uom": new_uom_norm,
                "old_cost": old_cost,
                "new_cost": new_cost,
                "abs_delta": abs_delta,
                "pct_change": pct_change,
                "sell_price": sell_price if sell_price is not None else "",
                "effective_date": args.effective_date,
            })

    if not args.no_flag_discontinued:
        for item in erp_items:
            erp_sku = item.get("sku", "")
            if erp_sku not in matched_erp_skus:
                exceptions.append({
                    "vendor_part_no": item.get("vendor_part_no", ""),
                    "erp_sku": erp_sku,
                    "exception_type": "DISCONTINUED_SKU",
                    "old_cost": fmt_dec(to_decimal(item.get("current_cost")), 4),
                    "new_cost": "",
                    "pct_change": "",
                    "old_uom": normalize_uom(item.get("uom", ""), alias_map),
                    "new_uom": "",
                    "confidence": "HIGH",
                    "source_page": "erp_master_unmatched",
                })

    avg_pct = (
        (sum(pct_changes_for_avg, Decimal("0")) / Decimal(len(pct_changes_for_avg)))
        if pct_changes_for_avg
        else None
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    upload_csv = out_dir / "erp_ready_upload.csv"
    exc_csv = out_dir / "exceptions_review.csv"
    report_md = out_dir / "price_change_impact_report.md"

    write_erp_upload_csv(upload_csv, clean_rows, profile)
    write_exceptions_csv(exc_csv, exceptions)
    write_impact_report(
        report_md,
        source_file=extracted.get("source_file", ""),
        erp_profile_name=profile_name,
        matched_count=matched_count,
        clean_count=len(clean_rows),
        avg_pct_change=avg_pct,
        exceptions=exceptions,
        margin_erosion_rows=margin_erosion_rows,
        min_margin_pct=min_margin,
    )

    summary = {
        "status": "ok",
        "erp_profile": profile_name,
        "matched_count": matched_count,
        "erp_ready_count": len(clean_rows),
        "exceptions_count": len(exceptions),
        "average_pct_change": fmt_dec(avg_pct, 2) if avg_pct is not None else None,
        "outputs": {
            "erp_ready_upload": str(upload_csv),
            "exceptions_review": str(exc_csv),
            "price_change_impact_report": str(report_md),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
