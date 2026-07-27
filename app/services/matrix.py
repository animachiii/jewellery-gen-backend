"""Real Sheets-backed matrix reader for `GET /api/v1/matrix` and the worker's
`resolve` stage.

Phase 3 replaces Phase 1's `_STUB_ROWS`/fixture-backed stub with a real read
of the client's hand-authored pivot grid (`docs/schema.md` §2), cached in
Redis with a TTL (`matrix:data`) and a version hash (`matrix:version`, no
TTL). Parsing is delegated entirely to `app/services/matrix_parser.py`
(extracted from `scripts/validate_matrix.py`, already validated against the
real sheet) — this module never re-derives the pivot-grid layout logic.

Never includes prompt text in `get_matrix_response()` — the client's IP is
never exposed through the client-facing API (docs/api-routes.md → Matrix).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.config import settings
from app.core.logging import get_logger
from app.models.enums import JewelryType, ServiceType
from app.models.job import _iso
from app.models.schemas import MatrixCombination, MatrixResponse
from app.services.matrix_parser import Variant, parse_pivot_matrix
from app.store.sheets_store import SheetsClient

log = get_logger(__name__)

_DATA_KEY = "matrix:data"
_VERSION_KEY = "matrix:version"

# Mirrors app/store/sheets_store.py's `_with_lock` SET-NX-EX + retry-sleep
# shape. Not imported directly: `_with_lock` there is private to that module
# and scoped to `lock:sheets:write`; this is a small, self-contained pattern
# and the codebase already tolerates this exact duplication (see that
# module's own docstring on _with_lock).
_LOCK_KEY = "lock:matrix:refresh"
_LOCK_TTL_SECONDS = 30
_LOCK_RETRY_DELAY_SECONDS = 0.2
_LOCK_MAX_ATTEMPTS = 100  # ~20s worst case, comfortably above the 30s lock TTL


class MatrixUnavailableError(Exception):
    """Sheets unreachable (or no client configured) and the Redis cache is
    cold — docs/schema.md's `MATRIX_UNAVAILABLE` (retryable), distinct from a
    `MATRIX_MISS` (a real content gap, not an infra problem)."""


@dataclass
class MatrixRow:
    prompt: str
    negative_prompt: str | None
    reference_url: str
    params: dict[str, object] | None


async def _with_matrix_lock(redis: Redis, fn: object) -> object:
    for _ in range(_LOCK_MAX_ATTEMPTS):
        acquired = await redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECONDS)
        if acquired:
            try:
                return await fn()  # type: ignore[operator]
            finally:
                await redis.delete(_LOCK_KEY)
        await asyncio.sleep(_LOCK_RETRY_DELAY_SECONDS)
    raise TimeoutError("Timed out waiting for lock:matrix:refresh")


def _variant_to_dict(v: Variant) -> dict[str, object]:
    return {
        "jewelry_type": v.jewelry_type,
        "service": v.service,
        "row_number": v.row_number,
        "prompt": v.prompt,
        "reference_image_url": v.reference_image_url,
    }


async def _get_cached_variants(redis: Redis) -> list[dict[str, object]] | None:
    raw = await redis.get(_DATA_KEY)
    if raw is None:
        return None
    result: list[dict[str, object]] = json.loads(raw)
    return result


def _compute_version(variants: list[Variant]) -> str:
    """docs/business-rules.md §5: "sha256 of all active matrix rows serialised
    in sorted key order, truncated to 16 hex chars." Every successfully
    parsed Variant is implicitly active (docs/schema.md §2 — the sheet has no
    `active` column; non-blank = active).

    Concrete scheme (documented here since "sorted key order" is otherwise
    underspecified): sort variants by (jewelry_type, service, row_number),
    then join each as "jewelry_type|service|row_number|prompt|reference_url"
    with "\\n", and sha256-hash the resulting UTF-8 string, truncated to the
    first 16 hex characters. Deterministic and reproducible by anyone re-
    running scripts/validate_matrix.py against the same sheet state.
    """
    ordered = sorted(variants, key=lambda v: (v.jewelry_type, v.service, v.row_number))
    canonical = "\n".join(
        f"{v.jewelry_type}|{v.service}|{v.row_number}|{v.prompt}|{v.reference_image_url or ''}"
        for v in ordered
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def refresh_matrix(
    redis: Redis,
    sheets_client: SheetsClient | None,
    sheet_id: str,
    tab: str = "Sheet1",
    *,
    force: bool = False,
) -> tuple[str, int, bool]:
    """Returns (matrix_version, rows_loaded, changed).

    `force=False` (the default, used by resolve/version/response callers):
    if `matrix:data` is still warm, returns the cached version/count with
    `changed=False` and never touches Sheets. `force=True` (the admin
    refresh route) always bypasses the freshness check and does a real read.

    A `lock:matrix:refresh` lock (30s TTL) guards the actual read+parse+write
    so many simultaneous cache-miss callers don't thunder into Sheets at
    once.
    """
    if not force:
        cached = await _get_cached_variants(redis)
        if cached is not None:
            version = await redis.get(_VERSION_KEY) or ""
            return version, len(cached), False

    if sheets_client is None:
        raise MatrixUnavailableError("Sheets client unavailable and matrix cache is cold")

    async def _do() -> tuple[str, int, bool]:
        if not force:
            # Someone else may have refreshed while we waited for the lock.
            cached = await _get_cached_variants(redis)
            if cached is not None:
                version = await redis.get(_VERSION_KEY) or ""
                return version, len(cached), False

        raw_values = await asyncio.to_thread(sheets_client.read_all_rows, sheet_id, tab)
        _, _, variants, _ = parse_pivot_matrix(raw_values)

        version = _compute_version(variants)
        previous_version = await redis.get(_VERSION_KEY)
        changed = previous_version != version

        payload = json.dumps([_variant_to_dict(v) for v in variants])
        await redis.set(_DATA_KEY, payload, ex=settings.matrix_cache_ttl)
        await redis.set(_VERSION_KEY, version)

        log.info(
            "matrix.refreshed",
            rows_loaded=len(variants),
            matrix_version=version,
            changed=changed,
            forced=force,
        )
        return version, len(variants), changed

    result = await _with_matrix_lock(redis, _do)
    return result  # type: ignore[return-value]


async def _ensure_cached_variants(
    redis: Redis, sheets_client: SheetsClient | None, sheet_id: str
) -> list[dict[str, object]]:
    variants = await _get_cached_variants(redis)
    if variants is not None:
        return variants

    try:
        await refresh_matrix(redis, sheets_client, sheet_id, force=False)
    except MatrixUnavailableError:
        raise
    except Exception as exc:
        raise MatrixUnavailableError(str(exc)) from exc

    variants = await _get_cached_variants(redis)
    if variants is None:
        raise MatrixUnavailableError("Matrix cache still cold after refresh attempt")
    return variants


async def resolve_matrix_row(
    redis: Redis,
    sheets_client: SheetsClient | None,
    sheet_id: str,
    jewelry_type: JewelryType,
    service: ServiceType,
) -> MatrixRow | None:
    """Worker-internal matrix lookup used by the resolve stage. Returns None
    on a genuine miss (-> MATRIX_MISS, R9). Raises MatrixUnavailableError if
    Sheets is unreachable and the cache is cold (-> MATRIX_UNAVAILABLE,
    distinct from a content gap). Picks one variant at random when several
    are stacked for the same (jewelry_type, service) — the client's sheet has
    multiple prompt variants per combination by design (docs/schema.md §2)."""
    variants = await _ensure_cached_variants(redis, sheets_client, sheet_id)

    matches = [
        v
        for v in variants
        if v["jewelry_type"] == jewelry_type.value and v["service"] == service.value
    ]
    if not matches:
        return None

    chosen = random.choice(matches)
    return MatrixRow(
        prompt=str(chosen["prompt"]),
        negative_prompt=None,
        reference_url=str(chosen["reference_image_url"] or ""),
        params=None,
    )


async def current_matrix_version(
    redis: Redis, sheets_client: SheetsClient | None, sheet_id: str
) -> str:
    version = await redis.get(_VERSION_KEY)
    if version is not None:
        return str(version)
    version, _, _ = await refresh_matrix(redis, sheets_client, sheet_id, force=False)
    return str(version)


async def get_matrix_response(
    redis: Redis, sheets_client: SheetsClient | None, sheet_id: str
) -> MatrixResponse:
    variants = await _ensure_cached_variants(redis, sheets_client, sheet_id)
    version = await redis.get(_VERSION_KEY) or ""

    pairs = sorted({(str(v["jewelry_type"]), str(v["service"])) for v in variants})
    # Built explicitly field-by-field (jewelry_type, service only) so the
    # never-includes-prompt-text guarantee is structurally obvious here, not
    # just incidentally true because the source data happened to lack a
    # prompt field (it doesn't — `variants` carries real prompt text).
    combinations = [
        MatrixCombination(jewelry_type=JewelryType(jt), service=ServiceType(svc))
        for jt, svc in pairs
    ]
    jewelry_types = sorted({c.jewelry_type for c in combinations}, key=lambda t: t.value)
    services = sorted({c.service for c in combinations}, key=lambda s: s.value)

    return MatrixResponse(
        matrix_version=str(version),
        cached_at=_iso(datetime.now(UTC)),
        combinations=combinations,
        jewelry_types=jewelry_types,
        services=services,
    )
