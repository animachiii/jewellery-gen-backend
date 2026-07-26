import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.validate_matrix import parse_pivot_matrix, validate  # noqa: E402

HEADER = ["", "Anklets", "Necklace", "Earrings", "Bangles", "Bracelets", "Hipbelt", "Ring"]


def _prompt(text: str, url: str | None = "https://drive.google.com/file/d/abc123/view") -> str:
    return f"{text} {url}" if url else text


def test_clean_matrix_has_no_hard_failures() -> None:
    values = [
        HEADER,
        ["Female Model"],
        ["Traditional", _prompt("A ring prompt")],
        [],
        ["", "", "", "", "", "", "", ""],
    ]
    # Ring is column index 7
    values[2] = ["Traditional"] + [""] * 6 + [_prompt("A ring prompt")]
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(values)
    report = validate(header_row, unresolved_headers, variants, unresolved_labels, check_urls=False)
    assert report.hard_failures == []
    assert report.coverage[("RING", "FEMALE_MODEL_TRADITIONAL")] is True


def test_multiple_variants_are_not_a_duplicate_failure() -> None:
    values = [
        HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + [_prompt("variant one")],
        [""] * 7 + [_prompt("variant two")],
    ]
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(values)
    report = validate(header_row, unresolved_headers, variants, unresolved_labels, check_urls=False)
    assert report.hard_failures == []
    assert report.variant_counts[("RING", "FEMALE_MODEL_TRADITIONAL")] == 2


def test_missing_reference_url_is_a_hard_failure() -> None:
    values = [
        HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + [_prompt("no url here", url=None)],
    ]
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(values)
    report = validate(header_row, unresolved_headers, variants, unresolved_labels, check_urls=False)
    assert report.hard_failures == ["1 variant(s) missing a reference URL"]


def test_empty_prompt_is_a_hard_failure() -> None:
    values = [
        HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + ["https://drive.google.com/file/d/abc123/view"],
    ]
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(values)
    report = validate(header_row, unresolved_headers, variants, unresolved_labels, check_urls=False)
    assert report.hard_failures == ["1 variant(s) with an empty prompt"]


def test_unresolved_header_is_a_hard_failure() -> None:
    bad_header = ["", "Toe Rings"]
    values = [bad_header]
    header_row, unresolved_headers, variants, unresolved_labels = parse_pivot_matrix(values)
    report = validate(header_row, unresolved_headers, variants, unresolved_labels, check_urls=False)
    assert report.hard_failures == ["1 unresolved jewelry_type header(s)"]
