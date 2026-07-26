"""GET /api/v1/matrix — available Type x Service combinations.

Backed by `app/services/matrix.py`'s `StubMatrix` fixture in Phase 1; Phase 3
swaps that module's internals for a real Sheets read behind the same
`get_matrix_response()` call, so this route does not change.
"""

from fastapi import APIRouter, Depends

from app.api.deps import rate_limit, require_client_key
from app.models.schemas import MatrixResponse
from app.services.matrix import get_matrix_response

router = APIRouter()


@router.get("/matrix", response_model=MatrixResponse)
async def get_matrix(
    key_name: str = Depends(require_client_key),
    _rate_limited: None = Depends(rate_limit),
) -> MatrixResponse:
    return await get_matrix_response()
