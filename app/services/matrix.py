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
from datetime import UTC, datetime
from pathlib import Path

from app.models.enums import JewelryType, ServiceType
from app.models.job import _iso
from app.models.schemas import MatrixCombination, MatrixResponse

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "matrix.json"


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
