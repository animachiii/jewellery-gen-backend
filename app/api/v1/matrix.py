"""GET /api/v1/matrix — available Type x Service combinations.

Phase 3: backed by a real Sheets read of Sheet1 (app/services/matrix.py),
cached in Redis. This route's shape is unchanged from Phase 1's fixture-
backed stub.
"""

from fastapi import APIRouter, Depends, Request

from app.api.deps import rate_limit, require_client_key
from app.config import settings
from app.core.logging import get_logger
from app.models.schemas import MatrixResponse
from app.services.matrix import get_matrix_response
from app.store.sheets_store import GoogleSheetsClient, SheetsClient

router = APIRouter()
log = get_logger(__name__)


def _build_sheets_client() -> SheetsClient | None:
    """Mirrors app/api/v1/generate.py's `_build_sheets_client` — see that
    module's comment for why this small duplication is preferred here over a
    shared helper."""
    try:
        return GoogleSheetsClient(settings.google_service_account_info)
    except Exception:
        log.warning("sheets.client.unavailable")
        return None


@router.get("/matrix", response_model=MatrixResponse)
async def get_matrix(
    request: Request,
    key_name: str = Depends(require_client_key),
    _rate_limited: None = Depends(rate_limit),
) -> MatrixResponse:
    redis = request.app.state.redis
    sheets_client = _build_sheets_client()
    return await get_matrix_response(redis, sheets_client, settings.google_sheet_id)
