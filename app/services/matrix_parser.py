"""Shared pivot-grid parser for the client's `Sheet1` prompt matrix.

Extracted from `scripts/validate_matrix.py` (Phase 0a) so both the standalone
validation CLI and `app/services/matrix.py`'s real Sheets-backed reader
(Phase 3) share the exact same, already-validated-against-the-real-sheet
parsing logic. Do not re-derive this parsing model elsewhere — the client's
sheet layout is a hand-authored pivot grid, not a flat table (see
`docs/schema.md` §2 for the full layout description).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import JewelryType

REFERENCE_URL_RE = re.compile(r"(https://drive\.google\.com/\S+)\s*$")

# Client's sheet header text -> our JewelryType. Sheet wins; this map is the
# reconciliation point if the client renames a column header.
HEADER_TO_JEWELRY_TYPE: dict[str, JewelryType] = {
    "anklets": JewelryType.ANKLET,
    "necklace": JewelryType.NECKLACE,
    "earrings": JewelryType.EARRING,
    "bangles": JewelryType.BANGLE,
    "bracelets": JewelryType.BRACELET,
    "hipbelt": JewelryType.HIPBELT,
    "ring": JewelryType.RING,
}

CATEGORY_LABELS: dict[str, str] = {
    "female model": "FEMALE_MODEL",
    "male model": "MALE_MODEL",
    "mannequin - only in necklace": "MANNEQUIN",
    "product styling": "PRODUCT_STYLING",
}

STYLE_LABELS: dict[str, str] = {
    "traditional": "TRADITIONAL",
    "modern": "MODERN",
}


@dataclass
class Variant:
    jewelry_type: str
    service: str
    row_number: int
    prompt: str
    reference_image_url: str | None


def _split_prompt_and_url(cell: str) -> tuple[str, str | None]:
    match = REFERENCE_URL_RE.search(cell.strip())
    if not match:
        return cell.strip(), None
    url = match.group(1)
    prompt = cell[: match.start()].strip()
    return prompt, url


def parse_pivot_matrix(
    raw_values: list[list[str]],
) -> tuple[list[str], list[str], list[Variant], list[tuple[int, str]]]:
    """Returns (header_row, unresolved_header_names, variants, unresolved_service_labels)."""
    if not raw_values:
        return [], [], [], []

    header_row = raw_values[0]
    # column index (1-based within row) -> JewelryType.value
    col_to_type: dict[int, str] = {}
    unresolved_headers: list[tuple[int, str]] = []
    for col_idx in range(1, len(header_row)):
        name = header_row[col_idx].strip()
        if not name:
            continue
        jt = HEADER_TO_JEWELRY_TYPE.get(name.lower())
        if jt is None:
            unresolved_headers.append((1, name))
        else:
            col_to_type[col_idx] = jt.value

    variants: list[Variant] = []
    unresolved_service_labels: list[tuple[int, str]] = []

    current_category: str | None = None
    current_style: str | None = None
    block_start: int | None = None  # 0-indexed into raw_values

    def close_block(end_idx: int) -> None:
        """Emit variants for raw_values[block_start:end_idx] under the open category/style."""
        if block_start is None or current_category is None or current_style is None:
            return
        service = f"{current_category}_{current_style}"
        for col_idx, jt_value in col_to_type.items():
            for r in range(block_start, end_idx):
                row = raw_values[r]
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip()
                if not cell:
                    continue
                prompt, url = _split_prompt_and_url(cell)
                variants.append(
                    Variant(
                        jewelry_type=jt_value,
                        service=service,
                        row_number=r + 1,
                        prompt=prompt,
                        reference_image_url=url,
                    )
                )

    for i, row in enumerate(raw_values[1:], start=1):
        col_a = row[0].strip() if row else ""
        key = col_a.lower()

        if key in CATEGORY_LABELS:
            close_block(i)
            current_category = CATEGORY_LABELS[key]
            current_style = None
            block_start = None
            continue

        if key in STYLE_LABELS:
            close_block(i)
            if current_category is None:
                unresolved_service_labels.append((i + 1, col_a))
                current_style = None
                block_start = None
                continue
            current_style = STYLE_LABELS[key]
            block_start = i
            continue

        if col_a and key not in CATEGORY_LABELS and key not in STYLE_LABELS:
            unresolved_service_labels.append((i + 1, col_a))

        # blank colA: continuation row of the current block, nothing to do here

    close_block(len(raw_values))

    return header_row, [h for _, h in unresolved_headers], variants, unresolved_service_labels
