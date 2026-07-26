"""Step 4 stub matrix source for `GET /api/v1/matrix`.

Phase 3 replaces this with a real Sheets-backed reader (`docs/schema.md` §2 —
the client's hand-authored pivot grid). This stub serves fixture rows from
`tests/fixtures/matrix.json` per the Phase 1 Step 4 spec, so the route has a
real (if fake) data source to exercise now, and so `app/services/matrix.py`
already exists at the import path Phase 3 will fill in.

Never includes prompt text — the fixture has no prompt fields at all, so
that's structurally guaranteed rather than filtered out here.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.models.enums import JewelryType, ServiceType
from app.models.job import _iso
from app.models.schemas import MatrixCombination, MatrixResponse

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "matrix.json"

_MATRIX_VERSION = "stub-v1"


@dataclass
class MatrixRow:
    prompt: str
    negative_prompt: str | None
    reference_url: str
    params: dict[str, object] | None


def _row(jewelry_type: JewelryType, service: ServiceType) -> MatrixRow:
    return MatrixRow(
        prompt=(
            f"A professional product photo of a {jewelry_type.value} styled for "
            f"{service.value}, studio lighting, plain background."
        ),
        negative_prompt=None,
        reference_url=f"https://drive.google.com/stub/{jewelry_type.value}/{service.value}",
        params=None,
    )


# Worker-internal stub matrix (distinct from the API-facing StubMatrix fixture
# above, which never carries prompt text). A small hardcoded set of combos —
# any combo not listed here is a MATRIX_MISS (R9).
_STUB_ROWS: dict[tuple[JewelryType, ServiceType], MatrixRow] = {
    (JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL): _row(
        JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL
    ),
    (JewelryType.RING, ServiceType.FEMALE_MODEL_MODERN): _row(
        JewelryType.RING, ServiceType.FEMALE_MODEL_MODERN
    ),
    (JewelryType.NECKLACE, ServiceType.PRODUCT_STYLING_TRADITIONAL): _row(
        JewelryType.NECKLACE, ServiceType.PRODUCT_STYLING_TRADITIONAL
    ),
    (JewelryType.BRACELET, ServiceType.MALE_MODEL_MODERN): _row(
        JewelryType.BRACELET, ServiceType.MALE_MODEL_MODERN
    ),
}


async def resolve_matrix_row(jewelry_type: JewelryType, service: ServiceType) -> MatrixRow | None:
    """Worker-internal matrix lookup used by the resolve stage. Returns None on
    a miss -> MATRIX_MISS (R9). Distinct from get_matrix_response(), which
    backs the API-facing GET /matrix route and never carries prompt text."""
    return _STUB_ROWS.get((jewelry_type, service))


def current_matrix_version() -> str:
    return _MATRIX_VERSION


async def get_matrix_response() -> MatrixResponse:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    combinations = [
        MatrixCombination(
            jewelry_type=JewelryType(c["jewelry_type"]),
            service=ServiceType(c["service"]),
        )
        for c in raw["combinations"]
    ]
    jewelry_types = sorted({c.jewelry_type for c in combinations}, key=lambda t: t.value)
    services = sorted({c.service for c in combinations}, key=lambda s: s.value)
    return MatrixResponse(
        matrix_version=raw["matrix_version"],
        cached_at=_iso(datetime.now(UTC)),
        combinations=combinations,
        jewelry_types=jewelry_types,
        services=services,
    )
