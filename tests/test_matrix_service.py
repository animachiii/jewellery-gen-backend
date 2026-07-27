"""Phase 3 Step 2 — real Sheets-backed matrix reader (app/services/matrix.py).

Exercises refresh_matrix/resolve_matrix_row/current_matrix_version against a
FakeSheetsClient populated with a synthetic pivot-grid sheet mirroring the
client's real Sheet1 layout (docs/schema.md §2). No test may call a real
external service (docs/conventions.md -> Testing).
"""

import random

import pytest
from redis.asyncio import Redis

from app.models.enums import JewelryType, ServiceType
from app.services.matrix import (
    MatrixUnavailableError,
    current_matrix_version,
    get_matrix_response,
    refresh_matrix,
    resolve_matrix_row,
)
from tests.fakes.fake_sheets_client import FakeSheetsClient

SHEET_ID = "sheet-1"
_HEADER = ["", "Anklets", "Necklace", "Earrings", "Bangles", "Bracelets", "Hipbelt", "Ring"]
_URL = "https://drive.google.com/file/d/abc123/view"


def _cell(text: str, url: str | None = _URL) -> str:
    return f"{text} {url}" if url else text


def _matrix_rows() -> list[list[str]]:
    """One category ("Female Model"), two styles ("Traditional" with two
    stacked RING variants to prove the random-choice behaviour, "Modern"
    with one). HIPBELT x MALE_MODEL_TRADITIONAL is never populated -> a
    genuine MATRIX_MISS."""
    return [
        _HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + [_cell("Ring female traditional prompt one")],
        [""] * 7 + [_cell("Ring female traditional prompt two")],
        ["Modern"] + [""] * 6 + [_cell("Ring female modern prompt")],
    ]


async def test_refresh_matrix_populates_cache(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    version, rows_loaded, changed = await refresh_matrix(redis, client, SHEET_ID)

    assert version
    assert len(version) == 16
    assert rows_loaded == 3  # two Traditional variants + one Modern variant
    assert changed is True
    assert await redis.get("matrix:data") is not None
    assert await redis.get("matrix:version") == version


async def test_refresh_matrix_changed_false_when_content_identical(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())
    await refresh_matrix(redis, client, SHEET_ID)

    # Force a second real read of identical content -> same version, changed=False.
    version, _, changed = await refresh_matrix(redis, client, SHEET_ID, force=True)

    assert changed is False
    assert version == await redis.get("matrix:version")


async def test_resolve_matrix_row_hit(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    row = await resolve_matrix_row(
        redis, client, SHEET_ID, JewelryType.RING, ServiceType.FEMALE_MODEL_MODERN
    )

    assert row is not None
    assert row.prompt == "Ring female modern prompt"
    assert row.reference_url == _URL


async def test_resolve_matrix_row_miss_returns_none(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    row = await resolve_matrix_row(
        redis, client, SHEET_ID, JewelryType.HIPBELT, ServiceType.MALE_MODEL_TRADITIONAL
    )

    assert row is None


async def test_resolve_matrix_row_picks_among_stacked_variants(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())
    random.seed(0)

    expected_prompts = {
        "Ring female traditional prompt one",
        "Ring female traditional prompt two",
    }
    seen: set[str] = set()
    for _ in range(30):
        row = await resolve_matrix_row(
            redis, client, SHEET_ID, JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL
        )
        assert row is not None
        assert row.prompt in expected_prompts
        seen.add(row.prompt)

    # Bounded non-determinism: over 30 draws from 2 stacked variants, both
    # should be reachable (astronomically unlikely otherwise).
    assert seen == expected_prompts


async def test_resolve_matrix_row_does_not_reread_within_cache_ttl(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    await resolve_matrix_row(
        redis, client, SHEET_ID, JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL
    )
    assert client.read_all_rows_calls == 1

    await resolve_matrix_row(
        redis, client, SHEET_ID, JewelryType.RING, ServiceType.FEMALE_MODEL_MODERN
    )
    assert client.read_all_rows_calls == 1  # still warm, no second read


async def test_resolve_matrix_row_raises_matrix_unavailable_when_no_client_and_cold(
    redis: Redis,
) -> None:
    with pytest.raises(MatrixUnavailableError):
        await resolve_matrix_row(
            redis, None, SHEET_ID, JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL
        )


async def test_current_matrix_version_refreshes_when_cold(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    version = await current_matrix_version(redis, client, SHEET_ID)

    assert version
    assert client.read_all_rows_calls == 1
    # Second call reads the now-warm cache, no second Sheets read.
    version_again = await current_matrix_version(redis, client, SHEET_ID)
    assert version_again == version
    assert client.read_all_rows_calls == 1


async def test_get_matrix_response_never_includes_prompt_text(redis: Redis) -> None:
    client = FakeSheetsClient(rows=_matrix_rows())

    response = await get_matrix_response(redis, client, SHEET_ID)

    assert response.matrix_version
    assert len(response.combinations) > 0
    dumped = response.model_dump_json().lower()
    assert "prompt" not in dumped
