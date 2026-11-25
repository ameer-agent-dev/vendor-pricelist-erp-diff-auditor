# Vendor Pricelist ERP Diff Auditor

Deterministic supplier price-change auditor that extracts SKUs, descriptions, units of measure (UOMs), and new net or list costs from manufacturer `.pdf`, `.xlsx`, or `.csv` price lists, diffs them in Python using exact `Decimal` math against your current ERP item master CSV, isolates UOM traps (`EA` vs. `C`/100 or `M`/1000) and margin breaches, and exports a clean ERP bulk-upload CSV alongside an exception review queue.

Built for **wholesale distributors, manufacturing purchasing teams, and ERP analysts** running Epicor Prophet 21 (`epicor_p21`), NetSuite, QuickBooks, or custom ERP item masters.

## Quick Install

```bash
npx skills add ameer-agent-dev/vendor-pricelist-erp-diff-auditor --skill vendor-pricelist-erp-diff-auditor
```

*(Single-repo shorthand: `npx skills add ameer-agent-dev/vendor-pricelist-erp-diff-auditor`)*

## Required & Optional Inputs

- **Mandatory**:
  - Incoming vendor price list (`.pdf`, `.xlsx`, or `.csv`).
  - Current ERP item master CSV (`sku`, `vendor_part_no`, `uom`, `current_cost`, and optional `sell_price`).
- **Optional**:
  - Trade-discount multiplier (e.g., `0.55`) when vendor sheets show List Price rather than Net Cost (gated automatically if omitted).
  - Target ERP upload schema YAML (defaults to `assets/erp_upload_schema_sample.yaml`: `epicor_p21`, `netsuite`, `quickbooks`).
  - Custom alert thresholds: `--max-increase-pct` (`10.0`), `--max-decrease-pct` (`20.0`), `--uom-swing-pct` (`40.0`), `--min-margin-pct` (`25.0`).

## Example Prompts

- **Normal run**:
  > "Extract net prices from `./2026_acme_valves_pricelist.xlsx`, diff them against `./erp_item_master.csv` using the `netsuite` profile, flag any hike above 10% or gross margin under 25%, and generate the clean ERP upload CSV."
- **Edge case (List Price PDF with `0.52` multiplier, leading-zero SKUs, `C`/100 UOM switch, and a scanned promo page)**:
  > "Diff `./supplier_list_catalog.pdf` (List Price with a `0.52` multiplier) against `./p21_master.csv` (`epicor_p21` profile). Preserve leading zeros on SKUs like `004512`, catch fasteners switching from `EA` to `C`, and isolate any scanned pages."

## Expected Output Files

- `extracted_pricelist.json`: Normalized extraction output with string-preserved part numbers, per-row confidence (`HIGH`, `MED`, `LOW`), and `scanned_pages` list.
- `erp_ready_upload.csv`: Schema-validated bulk import file containing **only** `HIGH`-confidence clean matches with quoted string SKUs (preserving leading zeros such as `"004512"`).
- `exceptions_review.csv`: 10-column purchasing review queue (`vendor_part_no, erp_sku, exception_type, old_cost, new_cost, pct_change, old_uom, new_uom, confidence, source_page`) isolating `UOM_MISMATCH`, `COST_SPIKE`, `COST_DROP_UOM_SUSPECT`, `MARGIN_BREACH`, `NEW_SKU`, `DISCONTINUED_SKU`, `MED_CONFIDENCE`/`LOW_CONFIDENCE`, and `SCANNED_PDF_REVIEW`.
- `price_change_impact_report.md`: Executive purchasing summary with matched SKU count, average % cost delta, exception breakdown, and top 20 margin-eroding items.

## Bundled Scripts, References & Assets

| Path | Purpose |
| :--- | :--- |
| `SKILL.md` | Extraction/diff workflow, multiplier gate, leading-zero rules, and exception isolation |
| `scripts/extract_pricelist.py` | Parses `.pdf`, `.xlsx`, and `.csv` vendor files with confidence scoring and scanned-page detection (`--help` supported) |
| `scripts/diff_erp_costs.py` | Runs exact `Decimal` cost/margin diffs, UOM normalization, and schema-mapped CSV export (`--help` supported) |
| `references/uom_normalization_table.yaml` | Canonical UOM alias map (`EA`, `C`, `M`, `BX`, `CS`, `PK`, `FT`, `LB`) and high-risk multiplier tokens |
| `assets/erp_upload_schema_sample.yaml` | Ready-to-use ERP bulk-import profiles for Epicor Prophet 21, NetSuite, and QuickBooks |

### Python Dependencies

Requires Python 3 with `pdfplumber`, `openpyxl`, `pandas`, and `PyYAML`:

```bash
pip install pdfplumber openpyxl pandas PyYAML
```

## Autonomy & Safety Boundary

- **Autonomous local operations**: Processes vendor PDFs, spreadsheets, and ERP CSVs locally offline as untrusted data; uses Python `Decimal` math; aborts automated pricing on scanned PDF pages (`SCANNED_PDF_REVIEW`); and keeps every suspect row out of `erp_ready_upload.csv`.
- **Human sign-off required**: Outputs static local CSV and Markdown files only. Never writes directly to live ERP databases or triggers automated cost imports (`disable-model-invocation: true` recommended when live ERP connectors are enabled). Purchasing sign-off is required before importing `erp_ready_upload.csv`.

## License

[MIT](LICENSE)
