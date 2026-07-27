#!/usr/bin/env python3
"""Validate the client's real prompt matrix (Sheet1) against our enums.

Standalone CLI, not part of the `app` package.

The client's actual sheet is a pivot grid, not a flat table:
  - Row 1: jewelry type headers (one column per JewelryType, e.g. "Anklets", "Ring")
  - Column A: section labels marking blocks — a category label ("Female Model",
    "Male Model", "Mannequin - only in necklace", "Product styling") followed by a
    style label ("Traditional" / "Modern"). Data for that (category, style) block
    starts on the style-label row itself and continues on subsequent blank-colA rows
    until the next label.
  - Each cell in a block is one prompt *variant* for that (jewelry_type, service)
    combination — an arbitrary number of blank/non-blank cells per column, since the
    sheet was filled in by hand. Multiple variants per combination are expected; the
    matrix resolver picks one at random at request time (not a duplicate-key error).
  - There is no separate reference_image_url column — each prompt cell ends with an
    embedded Google Drive link (`https://drive.google.com/...`), which is parsed out.
  - There are no `active`, `negative_prompt`, `provider_params`, or `updated_at`
    columns in the current sheet; those fields are treated as absent/None.

Usage:
    python scripts/validate_matrix.py [--sheet-id SHEET_ID] [--tab Sheet1]

Exits non-zero on any hard failure: unresolvable jewelry_type header, or a variant
whose prompt has no parseable reference URL.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.models.enums import JewelryType, ServiceType  # noqa: E402
from app.services.matrix_parser import (  # noqa: E402
    CATEGORY_LABELS,
    HEADER_TO_JEWELRY_TYPE,
    REFERENCE_URL_RE,
    STYLE_LABELS,
    Variant,
    _split_prompt_and_url,
    parse_pivot_matrix,
)

__all__ = [
    "CATEGORY_LABELS",
    "HEADER_TO_JEWELRY_TYPE",
    "REFERENCE_URL_RE",
    "STYLE_LABELS",
    "Variant",
    "_split_prompt_and_url",
    "parse_pivot_matrix",
]


@dataclass
class ValidationReport:
    header_ok: bool = True
    header_actual: list[str] = field(default_factory=list)
    unresolved_headers: list[tuple[int, str]] = field(default_factory=list)
    unresolved_service_labels: list[tuple[int, str]] = field(default_factory=list)
    missing_reference_url: list[tuple[int, str]] = field(default_factory=list)
    empty_prompt_variants: list[tuple[int, str]] = field(default_factory=list)
    variant_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    coverage: dict[tuple[str, str], bool] = field(default_factory=dict)
    unreachable_urls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[str]:
        failures = []
        if self.unresolved_headers:
            failures.append(f"{len(self.unresolved_headers)} unresolved jewelry_type header(s)")
        if self.missing_reference_url:
            failures.append(f"{len(self.missing_reference_url)} variant(s) missing a reference URL")
        if self.empty_prompt_variants:
            failures.append(f"{len(self.empty_prompt_variants)} variant(s) with an empty prompt")
        return failures


def fetch_sheet_values_real(sheet_id: str, tab: str) -> list[list[str]]:
    """Read the tab via the Sheets API using the configured service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{tab}!A1:Z1011")
        .execute()
    )
    values: list[list[str]] = result.get("values", [])
    return values


def validate(
    header_row: list[str],
    unresolved_header_names: list[str],
    variants: list[Variant],
    unresolved_service_labels: list[tuple[int, str]],
    *,
    check_urls: bool,
) -> ValidationReport:
    report = ValidationReport()
    report.header_actual = header_row
    report.header_ok = not unresolved_header_names
    report.unresolved_headers = [(1, h) for h in unresolved_header_names]
    report.unresolved_service_labels = unresolved_service_labels

    valid_types = {t.value for t in JewelryType}
    valid_services = {s.value for s in ServiceType}

    for v in variants:
        key = (v.jewelry_type, v.service)
        report.variant_counts[key] = report.variant_counts.get(key, 0) + 1
        if v.jewelry_type in valid_types and v.service in valid_services:
            report.coverage[key] = True
        if not v.prompt:
            report.empty_prompt_variants.append((v.row_number, f"{v.jewelry_type}/{v.service}"))
        if not v.reference_image_url:
            report.missing_reference_url.append((v.row_number, f"{v.jewelry_type}/{v.service}"))

    for jt in valid_types:
        for svc in valid_services:
            report.coverage.setdefault((jt, svc), False)

    if check_urls:
        unique_urls = {v.reference_image_url for v in variants if v.reference_image_url}
        for url in sorted(unique_urls):
            ok, reason = _check_url(url)
            if not ok:
                report.unreachable_urls.append((url, reason))

    return report


def _check_url(url: str) -> tuple[bool, str]:
    import ssl

    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        req = Request(url, method="HEAD")
        with urlopen(req, timeout=10, context=ctx) as resp:  # noqa: S310
            status = resp.status
            if status not in (200, 302, 303):
                return False, f"HTTP {status}"
            return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def print_report(report: ValidationReport) -> None:
    print("=" * 70)
    print("Matrix Validation Report (pivot-grid source)")
    print("=" * 70)

    print("\n1. Header check (jewelry_type columns)")
    if report.header_ok:
        print(f"   OK — {report.header_actual[1:]}")
    else:
        for _, name in report.unresolved_headers:
            print(f"   UNRESOLVED column header: '{name}'")

    print("\n2. Unresolved section/style labels in column A")
    if not report.unresolved_service_labels:
        print("   None")
    else:
        for row_num, label in report.unresolved_service_labels:
            print(f"   Row {row_num}: '{label}'")

    print("\n3. Variant counts per (jewelry_type, service)")
    populated = {k: v for k, v in report.variant_counts.items() if v > 0}
    if not populated:
        print("   None")
    else:
        for (jt, svc), count in sorted(populated.items()):
            print(f"   {jt:10s} x {svc:28s} -> {count} variant(s)")

    print("\n4. Coverage grid")
    types = sorted({k[0] for k in report.coverage})
    services = sorted({k[1] for k in report.coverage})
    if types and services:
        col_width = max(len(s) for s in services) + 2
        header_row = "jewelry_type".ljust(14) + "".join(s.ljust(col_width) for s in services)
        print("   " + header_row)
        for jt in types:
            line = jt.ljust(14)
            for svc in services:
                mark = "x" if report.coverage.get((jt, svc)) else "."
                line += mark.ljust(col_width)
            print("   " + line)
    else:
        print("   (no data)")

    print("\n5. Empty prompts / missing reference URLs")
    if not report.empty_prompt_variants and not report.missing_reference_url:
        print("   None")
    else:
        for row_num, k in report.empty_prompt_variants:
            print(f"   Row {row_num} ({k}): empty prompt")
        for row_num, k in report.missing_reference_url:
            print(f"   Row {row_num} ({k}): no parseable reference URL")

    print("\n6. Reference URL reachability")
    if not report.unreachable_urls:
        print("   All checked URLs OK (or check skipped)")
    else:
        for url, reason in report.unreachable_urls:
            print(f"   FAIL {url}: {reason}")

    print("\n" + "=" * 70)
    if report.hard_failures:
        print("HARD FAILURES:")
        for f in report.hard_failures:
            print(f"  - {f}")
    else:
        print("No hard failures.")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", default=settings.google_sheet_id)
    parser.add_argument("--tab", default="Sheet1")
    parser.add_argument(
        "--skip-url-check",
        action="store_true",
        help="Skip HEAD requests against reference_image_url (fast, no network)",
    )
    args = parser.parse_args()

    raw_values = fetch_sheet_values_real(args.sheet_id, args.tab)
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(raw_values)
    report = validate(
        header_row,
        unresolved_headers,
        variants,
        unresolved_labels,
        check_urls=not args.skip_url_check,
    )
    print_report(report)

    return 2 if report.hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
